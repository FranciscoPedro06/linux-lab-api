#!/usr/bin/env bash
# Cases of the copies off the host (infra/ops/remote.sh, backup.sh, sync-backups.sh,
# prune-backups.sh, backup-status.sh) against a real S3 API: a MinIO server started
# here on 127.0.0.1 and removed afterwards, the real rclone of the tested release,
# with a credential limited to PutObject and GetObject on one prefix, as on a host.
# Real: MinIO, rclone, age, package.py, the network between them (through a TCP
# relay of this script, which can refuse or cut connections). Stand-ins: PostgreSQL
# and Docker (infra/tests/stubs/docker), as in backup-cases.sh, and, in the lock
# cases only, a wrapper around rclone that starts a background process or waits.
#
#   infra/tests/remote-cases.sh
#
# Needs minio and mc (infra/tests/install-minio.sh), rclone (install-rclone.sh), age
# and age-keygen (install-age.sh), flock, timeout and python3. Keys, credentials
# and data are made for this run only and removed with it.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
ops="$here/../ops"
for program in minio mc rclone age age-keygen flock python3; do
    command -v "$program" >/dev/null || { echo "error: $program is required" >&2; exit 1; }
done
work=$(mktemp -d)
[ "$(stat -f -c %T /dev/shm)" = tmpfs ] || { echo "error: /dev/shm is not a tmpfs here" >&2; exit 1; }
shm=$(mktemp -d /dev/shm/remote-cases.XXXXXX)
pids=()
cleanup() {
    touch "$work/hold.release" "$work/wrap.release" 2>/dev/null || true
    local pid
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
    wait 2>/dev/null || true
    rm -rf "$work" "$shm"
}
trap cleanup EXIT
failures=0

# MinIO (infra/tests/minio.sh), its administrator (mc alias "admin", used by this
# script only, to look at and to damage objects) and the host's credential.
# shellcheck source-path=SCRIPTDIR source=minio.sh
source "$here/minio.sh"
minio_start "$work/minio"
pids+=("$MINIO_PID")
relay_port=$(free_port)
host_secret=$MINIO_HOST_SECRET

# The TCP relay between rclone and MinIO. Its mode file, read at each connection:
# "pass"; "down" (every connection reset at once: the destination is unreachable);
# "cut-up N" or "cut-down N" (a connection reset once N bytes went to MinIO, or came
# from it: a network failure in the middle of an upload or of a read).
cat >"$work/relay.py" <<'PY'
import asyncio, sys

listen, target, mode_file = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]

def mode():
    try:
        with open(mode_file) as source:
            return source.read().split() or ["pass"]
    except OSError:
        return ["pass"]

async def pipe(reader, writer, limit, both):
    sent = 0
    try:
        while data := await reader.read(65536):
            if limit is not None and sent + len(data) > limit:
                for w in both:
                    w.transport.abort()
                return
            writer.write(data)
            await writer.drain()
            sent += len(data)
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        for w in both:
            w.transport.abort()

async def handle(client_reader, client_writer):
    current = mode()
    if current[0] == "down":
        client_writer.transport.abort()
        return
    try:
        server_reader, server_writer = await asyncio.open_connection("127.0.0.1", target)
    except OSError:
        client_writer.transport.abort()
        return
    up = int(current[1]) if current[0] == "cut-up" else None
    down = int(current[1]) if current[0] == "cut-down" else None
    both = (client_writer, server_writer)
    await asyncio.gather(pipe(client_reader, server_writer, up, both),
                         pipe(server_reader, client_writer, down, both))
    for w in both:
        w.close()

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", listen)
    async with server:
        await server.serve_forever()

asyncio.run(main())
PY
relay() { echo "$*" >"$work/relay.mode"; }
relay pass
python3 "$work/relay.py" "$relay_port" "$MINIO_PORT" "$work/relay.mode" &
pids+=($!)

# The host's rclone configuration: this credential, through the relay. Private, as
# remote.sh requires.
mkdir -m 700 "$work/conf"
rclone_config="$work/conf/rclone.conf"
minio_rclone_config "$rclone_config" "$relay_port"
remote=offsite:backups/host
# host_rclone ARGS...: rclone as the host's credential, for the permission cases.
host_rclone() {
    rclone --config "$rclone_config" --s3-no-check-bucket --retries 1 --low-level-retries 1 "$@"
}

# What the scripts need besides the remote: the release file and keys of
# backup-cases.sh, Docker and PostgreSQL stood in for.
export PATH="$here/stubs:$PATH" STUB_LOG="$work/calls"
export LINUXLAB_ENV_FILE="$work/env" LINUXLAB_PROJECT=linuxlab-remote-cases
export LINUXLAB_TEST_LOCK_FILE="$work/ops.lock"
release="$work/release.env"
image_id=sha256:$(printf 'a%.0s' $(seq 64))
printf '%s\n' "API_SHA=0123456789abcdef0123456789abcdef01234567" \
    "WEB_SHA=fedcba9876543210fedcba9876543210fedcba98" \
    "API_IMAGE=api" "API_IMAGE_ID=$image_id" "LAB_IMAGE=lab" "LAB_IMAGE_ID=$image_id" \
    "EDGE_IMAGE=edge" "EDGE_IMAGE_ID=$image_id" >"$release"
keys="$work/keys"
mkdir -m 700 "$keys"
for key in one two; do age-keygen -o "$keys/$key.key" 2>/dev/null; done
printf '%s\n' "$(age-keygen -y "$keys/one.key")" "$(age-keygen -y "$keys/two.key")" >"$keys/recipients.txt"
chmod 600 "$keys/recipients.txt"
# What the stub's dump holds: it must never reach MinIO unencrypted.
plain_marker='rows of every table'

backups="$work/backups"
# reset [SETTING=VALUE...]: a fresh backup directory, work directory, call log and
# environment file, with the remote set (BACKUP_RETRIES 1, no delay) unless the
# arguments change it; later assignments win, as in an environment file.
reset() {
    rm -rf "$backups" "$shm/plain" "$work"/hold.* "$work"/wrap.*
    mkdir -p "$backups"
    mkdir -m 700 "$shm/plain"
    : >"$STUB_LOG"
    relay pass
    printf '%s\n' "BACKUP_DIR=$backups" "BACKUP_AGE_RECIPIENTS_FILE=$keys/recipients.txt" \
        "BACKUP_WORK_DIR=$shm/plain" "BACKUP_REMOTE=$remote" "BACKUP_RCLONE_CONFIG=$rclone_config" \
        "BACKUP_RETRIES=1" "BACKUP_RETRY_DELAY=0" "$@" >"$LINUXLAB_ENV_FILE"
}

run() {
    rc=0
    timeout 300 "$@" >"$work/out" 2>"$work/err" || rc=$?
    cat "$work/out" "$work/err" >>"$work/all.log"
}
check() {
    local check_name=$1
    shift
    if "$@"; then
        echo "ok    $check_name"
    else
        echo "FAIL  $check_name" >&2
        sed 's/^/      out: /' "$work/out" >&2
        sed 's/^/      err: /' "$work/err" >&2
        tail -n 5 "$STUB_LOG" | sed "s/^/      call: /" >&2
        failures=$((failures + 1))
    fi
}
not() { ! "$@"; }
in_dir() { (cd "$1" && "${@:2}"); }
failed() { [ "$rc" != 0 ]; }
succeeded() { [ "$rc" = 0 ]; }
err_says() { grep -q -- "$1" "$work/err"; }
out_says() { grep -q -- "$1" "$work/out"; }
called() { grep -q -- "$1" "$STUB_LOG"; }
not_called() { ! grep -q -- "$1" "$STUB_LOG"; }
# Calls of the stubs other than sync (stubs/sync logs too): Docker, so PostgreSQL.
no_docker_call() { ! grep -v -q "^sync " "$STUB_LOG"; }

backup() { run "$ops/backup.sh" "$release" "$@"; }
sync_backups() { run "$ops/sync-backups.sh"; }
prune() { run "$ops/prune-backups.sh"; }
status() { run "$ops/backup-status.sh"; [ "$rc" = 0 ]; }

# What MinIO holds, seen by its administrator.
object() { mc cat "admin/backups/host/$1" 2>/dev/null; }
exists() { mc stat "admin/backups/host/$1" >/dev/null 2>&1; }
object_sum() { object "$1" | sha256sum | cut -c1-64; }
object_size() { object "$1" | wc -c; }
object_time() { mc stat --json "admin/backups/host/$1" | python3 -c 'import json, sys; print(json.loads(sys.stdin.readline())["lastModified"])'; }
object_count() { mc ls --recursive admin/backups/host/ | wc -l; }
place() { mc pipe "admin/backups/host/$1" >/dev/null; }
drop() { mc rm "admin/backups/host/$1" >/dev/null; }
local_sum() { sha256sum <"$1" | cut -c1-64; }
same_as_local() { [ "$(object_sum "$(basename "$1")")" = "$(local_sum "$1")" ]; }

# fixture STAMP ID [BYTES]: an encrypted package of random bytes (encrypted to the
# recipients, so an age file like any backup) and its checksum, named as backup.sh
# names them; prints its path.
fixture() {
    local file="$backups/linuxlab-$1-$2-scheduled.tar.age"
    head -c "${3:-4096}" /dev/urandom | age --encrypt --recipients-file "$keys/recipients.txt" -o "$file"
    printf '%s  %s\n' "$(local_sum "$file")" "$(basename "$file")" >"$file.sha256"
    echo "$file"
}
# fingerprint FILE: the SHA-256 of a backup and its checksum, to show they did not change.
fingerprint() { cat "$1" "$1.sha256" | sha256sum | cut -c1-64; }
id16() { od -An -N8 -tx1 /dev/urandom | tr -d ' \n'; }
old_stamp() { date -u -d "$1 days ago" +%Y%m%dT%H%M%SZ; }

echo "== the host's credential (real MinIO, real rclone)"
echo "probe" >"$work/probe.txt"
check "it can create an object under the prefix" host_rclone copyto --no-check-dest "$work/probe.txt" "$remote/probe.txt"
check "  and read it" test "$(host_rclone cat --no-traverse "$remote/probe.txt")" = probe
check "  but not list the prefix" not host_rclone lsf "$remote/"
check "  nor delete an object" not host_rclone deletefile "$remote/probe.txt"
check "  which is still there" exists probe.txt
check "  nor write outside the prefix" not host_rclone copyto --no-check-dest "$work/probe.txt" offsite:backups/other/probe.txt
echo "other" >"$work/other.txt"
check "  nor replace an object with a conditional write (If-None-Match: *)" \
    not host_rclone copyto --no-check-dest --header-upload "If-None-Match: *" "$work/other.txt" "$remote/probe.txt"
check "  which keeps its content" test "$(object probe.txt)" = probe
drop probe.txt

echo "== backup.sh: a backup confirmed off the host"
reset
backup
package=$(cat "$work/out")
name=$(basename "$package")
check "the backup succeeds" succeeded
check "  stdout is still the package's path only" test -f "$package" -a "$(wc -l <"$work/out")" = 1
check "  the package is in MinIO (uploaded by rclone)" exists "$name"
check "  with the same SHA-256 as here" same_as_local "$package"
check "  and the same size" test "$(object_size "$name")" = "$(stat -c %s "$package")"
check "  its checksum file too, identical" same_as_local "$package.sha256"
check "  both were read back in full by the script" \
    test "$(grep -c 'uploaded and read back in full, identical' "$work/err")" = 3
line_of() { grep -n -F -- "$1" "$work/err" | head -n 1 | cut -d: -f1; }
in_order() {
    local package_line checksum_line receipt_line
    package_line=$(line_of "$name: uploaded and read back")
    checksum_line=$(line_of "$name.sha256: uploaded and read back")
    receipt_line=$(line_of "$name.receipt.json: uploaded and read back")
    [ -n "$package_line" ] && [ -n "$checksum_line" ] && [ -n "$receipt_line" ] \
        && [ "$package_line" -lt "$checksum_line" ] && [ "$checksum_line" -lt "$receipt_line" ]
}
check "  the package, then its checksum, then the receipt, each verified before the next" in_order
check "  the receipt here is the one in MinIO, byte for byte" \
    test "$(object "$name.receipt.json" | sha256sum | cut -c1-64)" = "$(local_sum "$package.receipt.json")"
receipt_fields() {
    python3 - "$package.receipt.json" "$package" "$remote" <<'PY'
import hashlib, json, os, sys
r = json.load(open(sys.argv[1]))
path = sys.argv[2]
data = open(path, "rb").read()
expected = {"format_version", "backup_id", "object", "size", "sha256", "checksum_object",
            "checksum_sha256", "destination", "rclone_version", "verified_at", "status"}
assert set(r) == expected, set(r) ^ expected
assert r["object"] == os.path.basename(path)
assert r["size"] == len(data) and r["sha256"] == hashlib.sha256(data).hexdigest()
assert r["checksum_sha256"] == hashlib.sha256(open(path + ".sha256", "rb").read()).hexdigest()
assert r["destination"] == sys.argv[3] and r["status"] == "verified"
assert r["rclone_version"] == "v1.75.2" and r["verified_at"].endswith("Z")
assert r["backup_id"] in r["object"]
PY
}
check "  the receipt says what was verified: id, object, size, SHA-256, time, rclone, destination" receipt_fields
check "  and nothing secret or from the manifest" \
    not grep -q -i -e "$host_secret" -e "$MINIO_HOST_USER" -e AGE-SECRET -e age1 -e alembic -e schema -e postgres "$package.receipt.json"
check "  no partial file is left" test -z "$(find "$backups" -name '*.partial')"
check "  backup-status.sh: nothing needs attention" status
check "  and names the backup as the latest confirmed" out_says "latest confirmed off the host: $name"
cp "$package.receipt.json" "$work/sample-receipt.json"

echo "== nothing unencrypted reaches MinIO"
backup restore-test --restore-test
check "a restore-tested backup is confirmed too" succeeded
only_encrypted_objects() {
    local object
    while read -r object; do
        case "$object" in
            *.tar.age) [ "$(object "$object" | head -c 21)" = "age-encryption.org/v1" ] || return 1 ;;
            *.tar.age.sha256) object "$object" | grep -Eq '^[0-9a-f]{64}  linuxlab-[^/]+\.tar\.age$' || return 1 ;;
            *.tar.age.receipt.json) object "$object" | python3 -c 'import json, sys; json.load(sys.stdin)' || return 1 ;;
            *) return 1 ;;
        esac
    done < <(mc ls --recursive --json admin/backups/host/ | python3 -c 'import json, sys; [print(json.loads(l)["key"]) for l in sys.stdin]')
}
check "every object is an age file, a checksum line or a receipt" only_encrypted_objects
any_object_says() {
    local key
    while read -r key; do
        object "$key" | grep -a -q -- "$1" && return 0
    done < <(mc ls --recursive --json admin/backups/host/ | python3 -c 'import json, sys; [print(json.loads(l)["key"]) for l in sys.stdin]')
    return 1
}
check "  no object holds the dump's data" not any_object_says "$plain_marker"
check "  nor does MinIO's storage on disk" not grep -r -a -q "$plain_marker" "$work/minio/data"

echo "== settings refused before anything is done"
for bad in "offsite:backups/host dir" "offsite:backups/../other" "offsite:backups/host;rm" \
    "offsite:backups/\$(id)""-offsite:backups" "offsite:" "offsite:backups//host" "offsite:backups/h*"; do
    reset "BACKUP_REMOTE=$bad"
    backup
    check "BACKUP_REMOTE '$bad' is refused" failed
    check "  before any dump" not_called pg_dump
done
reset "BACKUP_RCLONE_CONFIG="
backup
check "BACKUP_REMOTE without BACKUP_RCLONE_CONFIG is refused" failed
check "  and says so" err_says "BACKUP_RCLONE_CONFIG is not set"
chmod 640 "$rclone_config"
reset
backup
check "a configuration readable by its group is refused" failed
check "  before any dump" not_called pg_dump
check "  and its content is not shown" not grep -q "$host_secret" "$work/err"
chmod 600 "$rclone_config"
ln -s "$rclone_config" "$work/conf/link.conf"
reset "BACKUP_RCLONE_CONFIG=$work/conf/link.conf"
backup
check "a configuration that is a link is refused" failed
rm "$work/conf/link.conf"
mkdir -m 777 "$work/open"
cp -p "$rclone_config" "$work/open/rclone.conf"
reset "BACKUP_RCLONE_CONFIG=$work/open/rclone.conf"
backup
check "a configuration in a directory others can write is refused" failed
for bad in BACKUP_RETRIES=0 BACKUP_RETRIES=x BACKUP_RETRY_DELAY=-1 BACKUP_RETRY_DELAY=1s; do
    reset "$bad"
    backup
    check "$bad is refused before any dump" test "$rc" != 0 -a "$(grep -c pg_dump "$STUB_LOG")" = 0
done
reset BACKUP_COPY_COMMAND=true
backup
check "BACKUP_COPY_COMMAND is refused (no configurable command)" failed
check "  and says what replaces it" err_says "BACKUP_REMOTE"
check "  before any dump" not_called pg_dump
reset "BACKUP_REMOTE="
backup
check "without BACKUP_REMOTE the backup succeeds" succeeded
check "  and says it is on this host only" err_says "BACKUP_REMOTE not set: this backup exists on this host only"
check "  backup-status.sh reports it" not status
check "  as needing attention" out_says "ATTENTION  BACKUP_REMOTE is not set"

echo "== a backup directory whose path has spaces"
spaced="$work/back ups/a dir"
mkdir -p "$spaced"
reset "BACKUP_DIR=$spaced"
backup
check "the backup is confirmed off the host" succeeded
check "  with its receipt next to it" test -f "$(cat "$work/out").receipt.json"
check "  backup-status.sh reads it" status

echo "== the destination is unreachable: retries, then pending"
reset BACKUP_RETRIES=3 BACKUP_RETRY_DELAY=1
relay down
backup
package=$(cat "$work/out")
check "the backup fails (exit 1): no copy confirmed" failed
check "  three attempts, waiting 1 s then 2 s" \
    test "$(grep -c -e 'attempt 1 of 3 failed; next in 1 s' -e 'attempt 2 of 3 failed; next in 2 s' "$work/err")" = 2
check "  says it is not confirmed and stays here" err_says "NOT confirmed off the host"
check "  the package and its checksum are intact" in_dir "$backups" sha256sum --check --status "$(basename "$package").sha256"
check "  no receipt here" test ! -e "$package.receipt.json"
check "  the failure is recorded" test -s "$package.push-failed"
check "  nothing reached MinIO" not exists "$(basename "$package")"
fp=$(fingerprint "$package")
status || true
check "backup-status.sh: attention" failed
check "  a backup without a receipt" out_says "1 backup(s) here without a receipt"
check "  the failed copy" out_says "copy off the host failed"
check "  and no receipt at all" out_says "no backup has a valid receipt"
: >"$STUB_LOG"
relay pass
sync_backups
check "sync-backups.sh confirms it once the destination is back" succeeded
check "  without a new dump (no call to Docker at all)" no_docker_call
check "  the same package, unchanged" test "$(fingerprint "$package")" = "$fp"
check "  in MinIO, identical" same_as_local "$package"
check "  with its receipt here" test -f "$package.receipt.json"
check "  and the failure record removed" test ! -e "$package.push-failed"
check "  backup-status.sh: nothing needs attention" status

echo "== running sync-backups.sh again changes nothing"
when=$(object_time "$(basename "$package")")
count=$(object_count)
receipt_before=$(local_sum "$package.receipt.json")
sync_backups
check "a second run succeeds" succeeded
check "  with nothing to do" err_says "0 backup(s) without a receipt"
check "  no object added or rewritten" test "$(object_count)" = "$count" -a "$(object_time "$(basename "$package")")" = "$when"
check "  the receipt here is unchanged" test "$(local_sum "$package.receipt.json")" = "$receipt_before"
rm "$package.receipt.json"
sync_backups
check "without its receipt here, the backup is confirmed again" succeeded
check "  the objects already there are recognised as identical, not uploaded" \
    test "$(grep -c 'already off the host' "$work/err")" = 3
check "  no object added or rewritten" test "$(object_count)" = "$count" -a "$(object_time "$(basename "$package")")" = "$when"
check "  the receipt here is the one in MinIO" test "$(local_sum "$package.receipt.json")" = "$receipt_before"

echo "== an object of the same name with other content is never replaced"
reset
package=$(fixture "$(old_stamp 0)" "$(id16)")
name=$(basename "$package")
head -c 4096 /dev/urandom | place "$name"
other=$(object_sum "$name")
fp=$(fingerprint "$package")
sync_backups
check "a different object under the backup's name: not confirmed" failed
check "  reported as a conflict" err_says "$name: CONFLICT"
check "  not retried" not err_says "attempt 1 of"
check "  the object in MinIO is untouched" test "$(object_sum "$name")" = "$other"
check "  the backup here is unchanged, without a receipt" test "$(fingerprint "$package")" = "$fp" -a ! -e "$package.receipt.json"
check "  and the conflict recorded" grep -q conflict "$package.push-failed"

package=$(fixture "$(old_stamp 0)" "$(id16)")
name=$(basename "$package")
echo "0000000000000000000000000000000000000000000000000000000000000000  $name" | place "$name.sha256"
sync_backups
check "a different checksum object: not confirmed" failed
check "  reported as a conflict" err_says "$name.sha256: CONFLICT"
check "  no receipt here or there" test ! -e "$package.receipt.json" -a "$(exists "$name.receipt.json" && echo y)" != y

package=$(fixture "$(old_stamp 0)" "$(id16)")
name=$(basename "$package")
echo '{"status": "verified", "object": "something else"}' | place "$name.receipt.json"
sync_backups
check "a receipt object that is not this backup's: not confirmed" failed
check "  reported as a conflict" err_says "$name.receipt.json: CONFLICT"
check "  no receipt here" test ! -e "$package.receipt.json"
check "  the package and checksum in MinIO were verified and stay" same_as_local "$package"

echo "== a network failure in the middle of an upload"
reset
package=$(fixture "$(old_stamp 0)" "$(id16)" 20000000)
name=$(basename "$package")
fp=$(fingerprint "$package")
relay cut-up 5000000
sync_backups
check "the upload is cut after 5 MB: not confirmed" failed
check "  the upload failed" err_says "$name: upload failed"
check "  MinIO kept no partial object" not exists "$name"
check "  the backup here is unchanged, without a receipt" test "$(fingerprint "$package")" = "$fp" -a ! -e "$package.receipt.json"
relay pass
sync_backups
check "  once the network is back, sync-backups.sh confirms it" succeeded
check "  in full" same_as_local "$package"

echo "== a read back that fails in the middle"
package=$(fixture "$(old_stamp 0)" "$(id16)" 20000000)
name=$(basename "$package")
relay cut-down 5000000
sync_backups
check "the upload succeeds but the read back is cut: not confirmed" failed
check "  says it cannot be read back" err_says "$name: uploaded, but it cannot be read back"
check "  no receipt here or there" test ! -e "$package.receipt.json" -a "$(exists "$name.receipt.json" && echo y)" != y
relay pass
sync_backups
check "  read back in full later, it is confirmed" succeeded

echo "== the lock: one operation at a time, and rclone does not get it"
reset
STUB_HOLD="$work/hold" STUB_HOLD_ON="pg_dump -U" timeout 120 "$ops/backup.sh" "$release" \
    >"$work/held.out" 2>"$work/held.err" &
held=$!
for _ in $(seq 300); do [ -e "$work/hold.started" ] && break; sleep 0.1; done
fixture "$(old_stamp 0)" "$(id16)" >/dev/null
sync_backups
check "sync-backups.sh is refused while a backup runs" failed
check "  and says why" err_says "is running"
printf '%s\n' "BACKUP_LOCAL_RETENTION_DAYS=1" >>"$LINUXLAB_ENV_FILE"
prune
check "prune-backups.sh is refused too" failed
check "  and says why" err_says "is running"
touch "$work/hold.release"
held_rc=0
wait "$held" || held_rc=$?
check "the backup then ends, confirmed off the host" test "$held_rc" = 0

# A wrapper around the real rclone: at its first upload it starts a process that
# stays behind, then waits until it is released.
mkdir -p "$work/wrap-bin"
real_rclone=$(command -v rclone)
cat >"$work/wrap-bin/rclone" <<EOF
#!/usr/bin/env bash
case " \$* " in
    *" copyto "*)
        if [ ! -e "$work/wrap.started" ]; then
            sleep 120 &
            echo \$! >"$work/wrap.background"
            touch "$work/wrap.started"
            while [ ! -e "$work/wrap.release" ]; do sleep 0.1; done
        fi
        ;;
esac
exec "$real_rclone" "\$@"
EOF
chmod +x "$work/wrap-bin/rclone"
reset
fixture "$(old_stamp 0)" "$(id16)" >/dev/null
PATH="$work/wrap-bin:$PATH" timeout 120 "$ops/sync-backups.sh" >"$work/wrap.out" 2>"$work/wrap.err" &
held=$!
for _ in $(seq 300); do [ -e "$work/wrap.started" ] && break; sleep 0.1; done
sync_backups
check "a second sync-backups.sh is refused while one uploads" failed
check "  and says why" err_says "is running"
touch "$work/wrap.release"
held_rc=0
wait "$held" || held_rc=$?
check "the first one ends, confirmed" test "$held_rc" = 0
background=$(cat "$work/wrap.background")
holds_lock() {
    local fd
    for fd in /proc/"$1"/fd/*; do
        [ "$fd" -ef "$LINUXLAB_TEST_LOCK_FILE" ] && return 0
    done
    return 1
}
check "  the process rclone's wrapper left behind is still running" kill -0 "$background"
check "  and has no descriptor on the lock file" not holds_lock "$background"
sync_backups
check "  another operation gets the lock at once" succeeded
kill "$background" 2>/dev/null || true

echo "== prune-backups.sh: local retention"
reset
old_a=$(fixture "$(old_stamp 40)" "$(id16)")
old_b=$(fixture "$(old_stamp 35)" "$(id16)")
middle=$(fixture "$(old_stamp 20)" "$(id16)")
sync_backups
old_pending=$(fixture "$(old_stamp 30)" "$(id16)")
recent=$(fixture "$(old_stamp 1)" "$(id16)")
count=$(object_count)
packages() { find "$backups" -name '*.tar.age' | wc -l; }
prune
check "disabled by default: nothing removed" test "$rc" = 0 -a "$(packages)" = 5
check "  and says so" err_says "local retention is disabled"
echo "BACKUP_LOCAL_RETENTION_DAYS=7" >>"$LINUXLAB_ENV_FILE"
relay down
prune
check "the destination unreachable: the run stops (exit 1)" failed
check "  nothing removed" test "$(packages)" = 5
check "  and says why" err_says "local removal suspended"
relay pass
drop "$(basename "$old_a")"
prune
check "a receipt here whose object is gone off the host is not enough: stops" failed
check "  nothing removed" test "$(packages)" = 5
place "$(basename "$old_a")" < <(cat "$old_a")
head -c 100 "$old_b" | place "$(basename "$old_b")"
prune
check "an object truncated off the host: stops" failed
check "  says it differs" err_says "the copy off the host differs"
check "  nothing removed, not even the candidate confirmed before it" test "$(packages)" = 5
python3 -c 'import sys; d = bytearray(open(sys.argv[1], "rb").read()); d[200] ^= 1; sys.stdout.buffer.write(d)' "$old_b" | place "$(basename "$old_b")"
prune
check "an object with a byte changed off the host: stops" failed
check "  nothing removed" test "$(packages)" = 5
place "$(basename "$old_b")" < <(cat "$old_b")
prune
check "every candidate confirmed off the host now: the run succeeds" succeeded
check "  the backups older than 7 days with a receipt are removed here" \
    test ! -e "$old_a" -a ! -e "$old_a.sha256" -a ! -e "$old_b" -a ! -e "$old_b.sha256"
check "  their receipts stay, as the record of where they are" test -f "$old_a.receipt.json" -a -f "$old_b.receipt.json"
check "  the pending one (no receipt) stays, though old" test -f "$old_pending" -a -f "$old_pending.sha256"
check "  the recent one stays" test -f "$recent"
check "  the newest with a receipt stays, though older than 7 days" test -f "$middle"
check "  nothing was removed from MinIO" test "$(object_count)" = "$count"
reset BACKUP_LOCAL_RETENTION_DAYS=7
only=$(fixture "$(old_stamp 60)" "$(id16)")
sync_backups
prune
check "the newest backup with a receipt stays, however old" test "$rc" = 0 -a -f "$only"
newer_pending=$(fixture "$(old_stamp 50)" "$(id16)")
prune
check "  even when a newer backup exists without a receipt" test -f "$only" -a -f "$newer_pending"

echo "== backup-status.sh: an old confirmation"
reset BACKUP_MAX_AGE_HOURS=1
package=$(fixture "$(old_stamp 0)" "$(id16)")
sync_backups
python3 - "$package.receipt.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
r["verified_at"] = "2020-01-01T00:00:00Z"
json.dump(r, open(sys.argv[1], "w"))
PY
status || true
check "a confirmation older than BACKUP_MAX_AGE_HOURS needs attention" failed
check "  and says how old" out_says "BACKUP_MAX_AGE_HOURS 1"
echo "not json" >"$package.receipt.json"
status || true
check "a receipt that cannot be read needs attention" out_says "not a receipt that can be read"

echo "== nothing secret in any output"
check "the host's secret key never appears" not grep -q -e "$host_secret" -e "$MINIO_ROOT_PASSWORD" "$work/all.log"
echo "sample receipt:"
sed 's/^/  /' "$work/sample-receipt.json"

if [ "$failures" -gt 0 ]; then
    echo "$failures checks failed" >&2
    exit 1
fi
echo "all remote cases passed"
