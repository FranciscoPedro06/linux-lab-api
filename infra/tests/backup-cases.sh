#!/usr/bin/env bash
# Cases of infra/ops/backup.sh, restore.sh, migrate.sh and content-sync.sh with the
# docker CLI replaced by infra/tests/stubs/docker: no container or database is
# involved, and everything is written under a temporary directory. Packages are
# built, checked, encrypted and decrypted by the real infra/ops/package.py, tar, gzip,
# sha256sum and age, with keys made for this run only and removed with it; only
# PostgreSQL and Docker are stand-ins (infra/tests/e2e.sh runs the real ones). Each
# case checks what the script did (the files it left, the calls it made), not only
# its exit status.
#
#   infra/tests/backup-cases.sh
#
# Needs flock (util-linux), gzip, tar, timeout, python3, and age and age-keygen of
# the tested release (infra/release/install-age.sh).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
ops="$here/../ops"
work=$(mktemp -d)
# The work directory must be in memory (lib.sh): /dev/shm is a tmpfs, here and in CI.
[ "$(stat -f -c %T /dev/shm)" = tmpfs ] || { echo "error: /dev/shm is not a tmpfs here" >&2; exit 1; }
shm=$(mktemp -d /dev/shm/backup-cases.XXXXXX)
cleanup() {
    touch "$work/hold.release"
    wait
    rm -rf "$work" "$shm"
}
trap cleanup EXIT
export PATH="$here/stubs:$PATH" STUB_LOG="$work/calls"
export LINUXLAB_ENV_FILE="$work/env" LINUXLAB_PROJECT=linuxlab-backup-cases
export LINUXLAB_TEST_LOCK_FILE="$work/ops.lock"
backups="$work/backups"
plain_dir="$shm/plain"
release="$work/release.env"
api_sha=0123456789abcdef0123456789abcdef01234567
web_sha=fedcba9876543210fedcba9876543210fedcba98
image_id=sha256:$(printf 'a%.0s' $(seq 64))
printf '%s\n' "API_SHA=$api_sha" "WEB_SHA=$web_sha" \
    "API_IMAGE=api" "API_IMAGE_ID=$image_id" "LAB_IMAGE=lab" "LAB_IMAGE_ID=$image_id" \
    "EDGE_IMAGE=edge" "EDGE_IMAGE_ID=$image_id" >"$release"
HEAD='e6b40c7d9a18 (head)\n'
failures=0

# Keys for this run only: "one" (the operator) and "two" (offline recovery) are the
# recipients; "three" is not.
keys="$work/keys"
mkdir -m 700 "$keys"
for key in one two three; do age-keygen -o "$keys/$key.key" 2>/dev/null; done
printf '%s\n' "# operator" "$(age-keygen -y "$keys/one.key")" "" \
    "# offline recovery" "$(age-keygen -y "$keys/two.key")" >"$keys/recipients.txt"
chmod 600 "$keys/recipients.txt"

# A fresh backup directory, work directory, call log and environment file (extra
# settings as arguments, after the encryption settings every backup needs).
reset() {
    rm -rf "$backups" "$plain_dir" "$work/elsewhere" "$work/offsite" "$work"/hold.*
    mkdir -p "$backups"
    mkdir -m 700 "$plain_dir"
    : >"$STUB_LOG"
    printf '%s\n' "BACKUP_DIR=$backups" "BACKUP_AGE_RECIPIENTS_FILE=$keys/recipients.txt" \
        "BACKUP_WORK_DIR=$plain_dir" "$@" >"$LINUXLAB_ENV_FILE"
}

# run COMMAND...: exit status in $rc, standard output in $work/out, errors in $work/err;
# both also added to $work/all.log, which must never show a private key.
run() {
    rc=0
    timeout 120 "$@" >"$work/out" 2>"$work/err" || rc=$?
    cat "$work/out" "$work/err" >>"$work/all.log"
}

check() {
    local name=$1
    shift
    if "$@"; then
        echo "ok    $name"
    else
        echo "FAIL  $name" >&2
        sed 's/^/      out: /' "$work/out" >&2
        sed 's/^/      err: /' "$work/err" >&2
        failures=$((failures + 1))
    fi
}

not() { ! "$@"; }
failed() { [ "$rc" != 0 ]; }
succeeded() { [ "$rc" = 0 ]; }
called() { grep -q -- "$1" "$STUB_LOG"; }
not_called() { ! grep -q -- "$1" "$STUB_LOG"; }
count_calls() { grep -c -- "$1" "$STUB_LOG" || true; }
files() { find "$backups" -maxdepth 1 -type f -name "$1" | sort; }
none() { [ -z "$(files "$1")" ]; }
one() { [ "$(files "$1" | wc -l)" = 1 ]; }
# No partial file or directory, of any run.
no_partials() { [ -z "$(find "$backups" -mindepth 1 -maxdepth 1 -name '*.partial')" ]; }
# Nothing unencrypted left in the work directory.
work_empty() { [ -z "$(find "$plain_dir" -mindepth 1)" ]; }
err_says() { grep -q -- "$1" "$work/err"; }
is_age() { [ "$(head -c 21 "$1")" = "age-encryption.org/v1" ]; }
# Every file in BACKUP_DIR is an encrypted package or its checksum.
only_encrypted() {
    local file
    for file in $(files '*'); do
        case "$file" in
            *.tar.age) is_age "$file" || return 1 ;;
            *.tar.age.sha256) ;;
            *) return 1 ;;
        esac
    done
}

# wait_for FILE: up to 30 s.
wait_for() {
    for _ in $(seq 300); do
        [ -e "$1" ] && return 0
        sleep 0.1
    done
    echo "timed out waiting for $1" >&2
    return 1
}

# The stub's dump (see stubs/docker): intact, or missing its end.
dump_data='rows of every table'
stub_dump() {
    if [ "${1:-valid}" = truncated ]; then
        printf 'PGDMP-STUB\n%s\n' "$dump_data"
    else
        printf 'PGDMP-STUB\n%s\nEND %s\n' "$dump_data" "$(printf '%s' "$dump_data" | sha256sum | cut -c1-64)"
    fi
}

checksum() {
    printf '%s  %s\n' "$(sha256sum <"$1" | cut -c1-64)" "$(basename "$1")" >"$1.sha256"
}

# package_plain FILE [DUMP] [FACT=VALUE...]: an unencrypted package built by
# package.py, as backup.sh builds them, from the stub's dump, with the facts the
# stub answers (or those given).
package_plain() {
    local file=$1 mode=${2:-valid} stage
    shift $(($# > 1 ? 2 : 1))
    stage=$(mktemp -d "$work/fixture.XXXXXX")
    stub_dump "$mode" >"$stage/postgres.dump"
    local facts=(
        backup_id=20260101T031500Z-0123456789abcdef created_at=2026-01-01T03:15:00Z
        label=scheduled release_file=release.env "api_sha=$api_sha" "api_image_id=$image_id"
        api_image_revision= api_running_image_id= "web_sha=$web_sha" "edge_image_id=$image_id"
        web_image_revision= edge_running_image_id= database=linuxlab server_version=16.15
        server_version_num=160015 "pg_dump_version=pg_dump (PostgreSQL) 16.15"
        alembic_revision=e6b40c7d9a18 "schema_fingerprint=$(printf 'c%.0s' $(seq 64))"
        "content=5 $(printf 'd%.0s' $(seq 64))" roles=linuxlab:true "caddy_data=no volume"
    )
    local given
    for given in "$@"; do
        facts=("${facts[@]/#${given%%=*}=*/$given}")
    done
    mkdir -p "$(dirname "$file")"
    python3 "$ops/package.py" build "$file" --component "database=$stage/postgres.dump" \
        "${facts[@]/#/--fact=}"
    rm -rf "$stage"
}

# encrypt PLAIN FILE: FILE is PLAIN encrypted to both recipients, with its checksum.
encrypt() {
    age --encrypt --recipients-file "$keys/recipients.txt" --output "$2" "$1"
    rm -f "$1"
    checksum "$2"
}

# fixture FILE.tar.age [DUMP] [FACT=VALUE...]: an encrypted package and its checksum.
fixture() {
    package_plain "$1.plain" "${@:2}"
    encrypt "$1.plain" "$1"
}

# craft FILE.tar.age KIND: a package that package.py would never build, encrypted to
# the recipients with a matching checksum, so that only the package's own checks can
# refuse it.
craft() {
    package_plain "$1.plain"
    python3 - "$1.plain" "$2" <<'PY'
import io, sys, tarfile
path, kind = sys.argv[1], sys.argv[2]
with tarfile.open(path) as tar:
    entries = [(m, tar.extractfile(m).read()) for m in tar.getmembers()]
def add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
    for member, data in entries:
        if kind == "no-manifest" and member.name == "manifest.json":
            continue
        if kind == "component-changed" and member.name == "database/postgres.dump":
            data = data.replace(b"rows", b"rowz")
        add(tar, member.name, data)
    if kind == "traversal":
        add(tar, "../outside", b"x")
    if kind == "duplicate":
        add(tar, "database/postgres.dump", entries[1][1])
PY
    encrypt "$1.plain" "$1"
}

# decrypt FILE [KEY]: the package inside FILE, decrypted by age to $work/decrypted.tar.
decrypt() {
    rm -f "$work/decrypted.tar"
    age --decrypt --identity "$keys/${2:-one}.key" --output "$work/decrypted.tar" "$1"
}
manifest() { decrypt "$1" && tar -xOf "$work/decrypted.tar" manifest.json; }
# json FILE EXPRESSION: a value of the encrypted package's manifest, as JSON.
json() { manifest "$1" | python3 -c "import json, sys; m = json.load(sys.stdin); print(json.dumps($2))"; }

backup() { run "$ops/backup.sh" "$release" "$@"; }
# restore FILE [ARGUMENT...]: with the operator's key, then the arguments given.
restore() { run "$ops/restore.sh" "$release" "$1" --identity-file "$keys/one.key" "${@:2}"; }
schema_c=$(printf 'c%.0s' $(seq 64))

echo "== backup.sh: one encrypted package"
reset
backup
package=$(cat "$work/out")
check "a backup writes one encrypted package" succeeded
check "  stdout is its path, and only that" test -f "$package" -a "$(wc -l <"$work/out")" = 1
check "  named linuxlab-<time>-<id>-<label>.tar.age" \
    grep -Eq "^$backups/linuxlab-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}-scheduled\.tar\.age$" "$work/out"
check "  an age file" is_age "$package"
check "  encrypted to the two recipients (two X25519 stanzas)" \
    test "$(LC_ALL=C sed -n '/^--- /q;p' "$package" | grep -c '^-> X25519 ')" = 2
check "  with its checksum, of the encrypted bytes, naming the file" \
    test "$(cat "$package.sha256")" = "$(sha256sum <"$package" | cut -c1-64)  $(basename "$package")"
check "  nothing else in the directory, nothing unencrypted" only_encrypted
check "  exactly two files" test "$(files '*' | wc -l)" = 2
check "  no partial file or directory is left" no_partials
check "  nothing is left in the work directory" work_empty
decrypt "$package" one && cp "$work/decrypted.tar" "$work/by-one.tar"
decrypt "$package" two && cp "$work/decrypted.tar" "$work/by-two.tar"
check "  the operator's key alone decrypts it" test -s "$work/by-one.tar"
check "  the recovery key alone decrypts it" test -s "$work/by-two.tar"
check "  both get exactly the same bytes" cmp -s "$work/by-one.tar" "$work/by-two.tar"
check "  a key that is not a recipient does not" not decrypt "$package" three
check "  the package inside checks with package.py" python3 "$ops/package.py" check "$work/by-one.tar"
check "  it holds exactly manifest.json and the dump, as regular files" \
    test "$(tar -tvf "$work/by-one.tar" | awk '{print substr($1, 1, 1) " " $NF}' | sort | tr '\n' ,)" \
    = "- database/postgres.dump,- manifest.json,"
check "  the dump in it is the one pg_dump wrote" \
    test "$(tar -xOf "$work/by-one.tar" database/postgres.dump)" = "$(stub_dump)"
check "  its manifest is valid JSON" python3 -c "import json, sys; json.load(sys.stdin)" < <(manifest "$package")
check "  format_version 1" test "$(json "$package" 'm["format_version"]')" = 1
check "  the API and web commits of the release file" \
    test "$(json "$package" '[m["code"]["api"]["sha"], m["code"]["web"]["sha"]]')" = "[\"$api_sha\", \"$web_sha\"]"
check "  no content commit, and why" \
    test "$(json "$package" 'm["code"]["content"]["sha"] is None and "not recorded" in m["code"]["content"]["source"]')" = true
check "  the missions' fingerprint" test "$(json "$package" 'm["code"]["content"]["missions"]')" = 5
check "  the database, its server, pg_dump and Alembic revision" \
    test "$(json "$package" '[m["postgresql"][k] for k in ("database", "server_version_num", "pg_dump_version", "alembic_revision", "schema_fingerprint")]')" \
    = "[\"linuxlab\", 160015, \"pg_dump (PostgreSQL) 16.15\", \"e6b40c7d9a18\", \"$schema_c\"]"
check "  the dump's size and SHA-256" \
    test "$(json "$package" '[(c["path"], c["size"], c["sha256"]) for c in m["components"]]')" \
    = "[[\"database/postgres.dump\", $(stub_dump | wc -c), \"$(stub_dump | sha256sum | cut -c1-64)\"]]"
check "  Caddy's data absent, and why" test "$(json "$package" 'm["caddy"]["data"]')" = "\"absent: no volume linuxlab-backup-cases_caddy-data\""
check "  the encrypted file was written to disk before its name existed" \
    grep -Eq '^sync .*/linuxlab-[^ ]+\.tar\.age\.partial \(final absent\)$' "$STUB_LOG"
check "  and the directory after it existed" grep -Eq "^sync $backups \(final files: 2\)$" "$STUB_LOG"

for mode in truncated corrupt fail; do
    reset
    STUB_DUMP=$mode backup
    check "a $mode dump is rejected" failed
    check "  no package is presented as a backup" none 'linuxlab-*.tar*'
    check "  no partial file or directory is left" no_partials
    check "  nothing is left in the work directory" work_empty
    check "  nothing is printed as a backup" test ! -s "$work/out"
done

echo "== backup.sh: the encryption settings"
for case_ in "no setting:" "missing file:$keys/nowhere.txt"; do
    reset
    printf '%s\n' "BACKUP_DIR=$backups" "BACKUP_WORK_DIR=$plain_dir" \
        "BACKUP_AGE_RECIPIENTS_FILE=${case_#*:}" >"$LINUXLAB_ENV_FILE"
    backup
    check "recipients file, ${case_%%:*}: no backup" failed
    check "  before any dump" not_called "pg_dump -U"
    check "  nothing written" test -z "$(find "$backups" -mindepth 1)"
done
recipients_case() {
    local name=$1
    shift
    printf '%s\n' "$@" >"$work/r-$name.txt"
    chmod 600 "$work/r-$name.txt"
    reset "BACKUP_AGE_RECIPIENTS_FILE=$work/r-$name.txt"
    backup
    check "recipients file with $name: refused" failed
    check "  before any dump" not_called "pg_dump -U"
}
operator=$(age-keygen -y "$keys/one.key")
recovery=$(age-keygen -y "$keys/two.key")
recipients_case "one recipient" "$operator"
recipients_case "a recipient listed twice" "$operator" "$operator"
recipients_case "a line that is not a recipient" "$operator" "$recovery" "age1notakey"
recipients_case "a private key in it" "$operator" "$recovery" "$(grep AGE-SECRET-KEY "$keys/three.key")"
check "  and the private key is not printed" not grep -q AGE-SECRET-KEY "$work/err"
printf '%s\n' "$operator" "$recovery" >"$work/r-writable.txt"
chmod 666 "$work/r-writable.txt"
reset "BACKUP_AGE_RECIPIENTS_FILE=$work/r-writable.txt"
backup
check "recipients file writable by others: refused" failed
printf '%s\n' "$operator" "$recovery" "$(age-keygen -y "$keys/three.key")" >"$work/r-three.txt"
chmod 600 "$work/r-three.txt"
reset "BACKUP_AGE_RECIPIENTS_FILE=$work/r-three.txt"
backup
package=$(cat "$work/out")
check "three recipients: accepted" succeeded
check "  three stanzas, and the third key decrypts" \
    test "$(LC_ALL=C sed -n '/^--- /q;p' "$package" | grep -c '^-> X25519 ')" = 3
check "  the third key decrypts" decrypt "$package" three
# Its owner and the directories above it (infra/tests/key-files-cases.sh has the rest).
mkdir -m 770 "$work/shared"
printf '%s\n' "$operator" "$recovery" >"$work/shared/recipients.txt"
chmod 644 "$work/shared/recipients.txt"
reset "BACKUP_AGE_RECIPIENTS_FILE=$work/shared/recipients.txt"
backup
check "recipients file in a directory its group can change: refused" failed
check "  and says so" err_says "has mode 770"
check "  before any dump" not_called "pg_dump -U"
ln -s "$keys/recipients.txt" "$work/recipients-link.txt"
reset "BACKUP_AGE_RECIPIENTS_FILE=$work/recipients-link.txt"
backup
check "recipients file that is a link: refused" failed
check "  before any dump" not_called "pg_dump -U"

echo "== backup.sh: the work directory"
mkdir -m 700 "$work/disk"
reset "BACKUP_WORK_DIR=$work/disk/work"
backup
check "a work directory on $(stat -f -c %T "$work") (a disk): refused" failed
check "  and says so" err_says "not in memory (tmpfs or ramfs)"
check "  before any dump" not_called "pg_dump -U"
check "  nothing written: not in BACKUP_DIR" test -z "$(find "$backups" -mindepth 1)"
check "  nor in the disk directory" test -z "$(find "$work/disk/work" -mindepth 1)"
check "  nor anywhere else in memory instead" work_empty
reset
STUB_DATABASE_SIZE=1000000000000000 backup
check "not enough room for the unencrypted package: refused" failed
check "  and says so" err_says "not enough room in the work directory"
check "  before any dump" not_called "pg_dump -U"
check "  nothing written" test -z "$(find "$backups" -mindepth 1)"
reset
chmod 755 "$plain_dir"
backup
check "a work directory others can read: refused" failed
check "  before any dump" not_called "pg_dump -U"
reset
rmdir "$plain_dir"
ln -s "$work" "$plain_dir"
backup
check "a work directory that is a link: refused" failed
rm "$plain_dir"
reset
rmdir "$plain_dir"
backup
check "a missing work directory is made, mode 0700" test "$rc" = 0 -a "$(stat -c %a "$plain_dir")" = 700
check "  and left empty" work_empty
reset "BACKUP_WORK_DIR=relative/dir"
backup
check "a relative work directory: refused" failed

echo "== backup.sh: Caddy's data"
reset
STUB_CADDY=valid backup
package=$(cat "$work/out")
check "an intact Caddy archive is packaged" succeeded
decrypt "$package"
check "  as caddy/caddy-data.tar.gz" tar -tf "$work/decrypted.tar" caddy/caddy-data.tar.gz
caddy_lists() {
    tar -xOf "$work/decrypted.tar" caddy/caddy-data.tar.gz | tar -tzf - >"$work/caddy.list" && grep -q site.key "$work/caddy.list"
}
check "  which lists in full" caddy_lists
check "  the manifest says so" test "$(json "$package" 'm["caddy"]["data"]')" = '"included"'
check "  and why Caddy's config is not there" \
    test "$(json "$package" '"autosave.json" in m["caddy"]["config"]')" = true
check "  no key of Caddy's in BACKUP_DIR unencrypted" not grep -rq "certificate and key" "$backups"
for mode in truncated corrupt flipped fail; do
    reset
    STUB_CADDY=$mode backup
    check "a $mode Caddy archive is rejected" failed
    check "  no package is presented as a backup" none 'linuxlab-*.tar*'
    check "  no partial file or directory is left" no_partials
    check "  nothing is left in the work directory" work_empty
done

echo "== backup.sh: what the manifest records"
reset
STUB_ROLES=app:false,linuxlab:true backup
check "a database with roles other than linuxlab is refused (no globals in the package)" failed
check "  before any dump" not_called "pg_dump -U"
reset
STUB_REVISION_api=1111111111111111111111111111111111111111 backup
check "an API image labeled with another commit than the release file's is refused" failed
check "  before any dump" not_called "pg_dump -U"
reset
STUB_REVISION_edge=1111111111111111111111111111111111111111 backup
check "an edge image labeled with another commit than the release file's is refused" failed
reset
running=sha256:$(printf 'b%.0s' $(seq 64))
STUB_REVISION_api=$api_sha STUB_REVISION_edge=$web_sha STUB_RUNNING_api=$running STUB_RUNNING_caddy=$image_id backup
package=$(cat "$work/out")
check "matching labels and running containers are recorded" \
    test "$(json "$package" '[m["code"]["api"]["image_revision_label"], m["code"]["web"]["image_revision_label"], m["code"]["api"]["running_image_id"], m["code"]["web"]["running_image_id"]]')" \
    = "[\"$api_sha\", \"$web_sha\", \"$running\", \"$image_id\"]"
check "  a running API that is not the release's is reported" err_says "not this release's image"
reset
STUB_TABLES=f backup
package=$(cat "$work/out")
check "a database never migrated: no Alembic revision and no missions" \
    test "$(json "$package" '[m["postgresql"]["alembic_revision"], m["code"]["content"]["fingerprint"]]')" = "[null, null]"

echo "== backup.sh --restore-test"
reset
backup --restore-test
package=$(cat "$work/out")
check "the package is restored before it is encrypted" succeeded
check "  into a new database, compared with the manifest" err_says "matches the manifest"
check "  which is dropped" called "DROP DATABASE IF EXISTS restore_"
check "  stdout is still the encrypted package's path only" test -f "$package" -a "$(wc -l <"$work/out")" = 1
check "  nothing is left in the work directory" work_empty
reset
STUB_RESTORED_SCHEMA=$(printf 'e%.0s' $(seq 64)) backup --restore-test
check "a package that does not restore as its manifest says is not written" failed
check "  no package" none 'linuxlab-*.tar*'
check "  the test database is dropped" called "DROP DATABASE IF EXISTS restore_"
check "  nothing is left in the work directory" work_empty

echo "== backup.sh: without a destination off the host, and no removal"
reset "BACKUP_COPY_COMMAND=cp -t $work/offsite"
backup
check "BACKUP_COPY_COMMAND is refused: no configurable copy command" failed
check "  and says what replaces it" err_says "BACKUP_REMOTE"
check "  before any dump" not_called "pg_dump -U"
check "  no package" none 'linuxlab-*.tar*'

reset
fixture "$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.tar.age"
touch -d '30 days ago' "$backups"/linuxlab-20260101T031500Z-*
backup
check "without BACKUP_REMOTE the backup succeeds" succeeded
check "  and says it is on this host only" err_says "BACKUP_REMOTE not set: this backup exists on this host only"
check "  an old backup is not removed by backup.sh" \
    test "$(files 'linuxlab-20260101T031500Z-*' | wc -l)" = 2
check "  no receipt is written (no copy was confirmed)" none '*.receipt.json'

echo "== backup.sh: partial files, interruptions and arguments"
reset
stale="$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.tar.age.partial"
echo "an earlier run that never finished" >"$stale"
mkdir "$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.partial"
backup
check "partial files of an earlier run are not backups" test "$(cat "$work/out")" != "$stale"
check "  and are left as they are" test -f "$stale" -a -d "${stale%.tar.age.partial}.partial"
check "  and are reported" err_says "2 unfinished"

reset
backup Not-A-Label
check "an invalid label is refused" failed
check "  before any dump" not_called "pg_dump -U"
reset
backup scheduled extra
check "an extra argument is refused" failed
check "  before any dump" not_called "pg_dump -U"
reset
backup --restore-tset
check "an unknown option is refused" failed
check "  before any dump" not_called "pg_dump -U"

for at in "pg_dump -U" "-czf -"; do
    reset
    STUB_CADDY=valid STUB_HOLD="$work/hold" STUB_HOLD_ON=$at timeout 120 "$ops/backup.sh" "$release" \
        >"$work/out" 2>"$work/err" &
    pid=$!
    wait_for "$work/hold.started"
    kill -TERM "$pid"
    touch "$work/hold.release"
    rc=0
    wait "$pid" || rc=$?
    check "a backup interrupted during '$at' fails" failed
    check "  and leaves no package and no partial file or directory" \
        test -z "$(find "$backups" -mindepth 1)"
    check "  and nothing in the work directory" work_empty
done

reset
STUB_HOLD="$work/hold" STUB_HOLD_ON="pg_dump -U" "$ops/backup.sh" "$release" >/dev/null 2>&1 &
pid=$!
wait_for "$work/hold.started"
kill -KILL "$pid"
wait "$pid" 2>/dev/null || true
touch "$work/hold.release"
sleep 0.5
check "a backup killed outright (no trap runs) leaves nothing in BACKUP_DIR" \
    test -z "$(find "$backups" -mindepth 1)"
check "  but its run directory in the work directory" \
    test -n "$(find "$plain_dir" -mindepth 1 -maxdepth 1 -name 'run.*')"
backup
check "  which the next backup removes" err_says "removed 1 run director"
check "  and the work directory is empty after it" work_empty

echo "== backup.sh: names are never reused or replaced"
reset
backup
first=$(cat "$work/out")
backup
second=$(cat "$work/out")
check "two backups run back to back, same label: two names" \
    test -n "$first" -a -n "$second" -a "$first" != "$second"
check "  both kept" test "$(files 'linuxlab-*.tar.age' | wc -l)" = 2

sums() { sha256sum "$@" 2>&1; }
reset
fixed=20260101T031500Z-0123456789abcdef
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a backup under a given name" succeeded
mapfile -t earlier < <(files "*-$fixed-*")
before=$(sums "${earlier[@]}")
: >"$STUB_LOG"
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a second run under the same name fails" failed
check "  and says it replaces nothing" err_says "never replaced"
check "  every file of the earlier backup is unchanged" test "$(sums "${earlier[@]}")" = "$before"
check "  no file was added" test "$(files '*' | wc -l)" = 2
check "  no partial file or directory is left" no_partials
check "  nothing is left in the work directory" work_empty
restore "$backups/linuxlab-$fixed-scheduled.tar.age"
check "  the earlier backup still restores" succeeded

reset
taken="$backups/linuxlab-$fixed-scheduled.tar.age"
echo "a file that happens to have the package's name" >"$taken"
before=$(sums "$taken")
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a run whose package name is taken after its checksum was published fails" failed
check "  the existing file is unchanged" test "$(sums "$taken")" = "$before"
check "  the checksum it had just published is withdrawn" test ! -e "$taken.sha256"
check "  no partial file or directory is left" no_partials
check "  nothing is printed as a backup" test ! -s "$work/out"

reset
fixture "$backups/linuxlab-20260101T000000Z-aaaaaaaaaaaaaaaa-scheduled.tar.age"
mapfile -t valid < <(files 'linuxlab-*')
before=$(sums "${valid[@]}")
echo "taken" >"$taken"
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a failed publication leaves an earlier valid backup as it was" test "$(sums "${valid[@]}")" = "$before"
restore "$backups/linuxlab-20260101T000000Z-aaaaaaaaaaaaaaaa-scheduled.tar.age"
check "  and it still restores" succeeded

reset
run env LINUXLAB_TEST_BACKUP_RUN='../../etc-x' "$ops/backup.sh" "$release"
check "a run name with a path in it is refused" failed
check "  before any dump" not_called "pg_dump -U"

echo "== restore.sh: keys"
reset
fixture "$backups/x.tar.age"
for key in one two; do
    : >"$STUB_LOG"
    run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/$key.key"
    check "restored with key $key alone" succeeded
    check "  into a new database, compared with the manifest" err_says "matches the manifest"
    check "  nothing is left in the work directory" work_empty
done
: >"$STUB_LOG"
run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/three.key"
check "a key that is not a recipient is refused" failed
check "  as one age could not decrypt with" err_says "age could not decrypt it"
check "  before any database is created" not_called "CREATE DATABASE"
check "  nothing is left in the work directory" work_empty
: >"$STUB_LOG"
run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/three.key" --identity-file "$keys/two.key"
check "several keys, one of them a recipient: restored" succeeded
# Key files others could read or change are refused before age reads them
# (infra/tests/key-files-cases.sh has every case).
cp "$keys/one.key" "$work/open.key"
for case_ in "644:readable by others" "640:readable by its group"; do
    chmod "${case_%%:*}" "$work/open.key"
    : >"$STUB_LOG"
    run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$work/open.key"
    check "a key file ${case_#*:} (mode ${case_%%:*}): refused" failed
    check "  and says so" err_says "has mode ${case_%%:*}"
    check "  before anything is done" test ! -s "$STUB_LOG"
    check "  nothing in the work directory" work_empty
done
ln -s "$keys/one.key" "$work/link.key"
: >"$STUB_LOG"
run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$work/link.key"
check "a key file that is a link: refused" failed
check "  before anything is done" test ! -s "$STUB_LOG"
: >"$STUB_LOG"
run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/one.key" --identity-file "$work/link.key"
check "a valid key next to a refused one: refused all the same" failed
check "  before anything is done" test ! -s "$STUB_LOG"
: >"$STUB_LOG"
run "$ops/restore.sh" "$release" "$backups/x.tar.age"
check "no key given: refused" failed
check "  before anything is done" test ! -s "$STUB_LOG"
run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/missing.key"
check "a key file that does not exist: refused" failed
run "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file
check "--identity-file without a file: refused" failed
run env "AGE_IDENTITY=$(cat "$keys/one.key")" "$ops/restore.sh" "$release" "$backups/x.tar.age"
check "a key in the environment is not read" failed

echo "== restore.sh: the encrypted file and its checksum"
reset
fixture "$backups/x.tar.age"
rm "$backups/x.tar.age.sha256"
restore "$backups/x.tar.age"
check "a package without its checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar.age"
echo "tampered" >>"$backups/x.tar.age"
restore "$backups/x.tar.age"
check "a package changed after its checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar.age"
sed -i 's/[0-9a-f]\{64\}/'"$(printf '0%.0s' $(seq 64))"'/' "$backups/x.tar.age.sha256"
restore "$backups/x.tar.age"
check "a changed checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar.age"
sed -i 's/  x\.tar\.age$/  other.tar.age/' "$backups/x.tar.age.sha256"
restore "$backups/x.tar.age"
check "a checksum naming another file is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

# Damage the encrypted bytes and write a checksum that matches them: only age can
# refuse the file then.
reset
fixture "$backups/x.tar.age"
size=$(wc -c <"$backups/x.tar.age")
for case_ in "flipped:a byte of the encrypted payload changed" "truncated:the last 16 bytes cut" "header:a byte of the header changed"; do
    : >"$STUB_LOG"
    cp "$backups/x.tar.age" "$backups/d.tar.age"
    case "${case_%%:*}" in
        flipped) printf '\377' | dd of="$backups/d.tar.age" bs=1 seek=$((size - 40)) conv=notrunc 2>/dev/null ;;
        truncated) head -c $((size - 16)) "$backups/x.tar.age" >"$backups/d.tar.age" ;;
        header) printf 'Z' | dd of="$backups/d.tar.age" bs=1 seek=60 conv=notrunc 2>/dev/null ;;
    esac
    checksum "$backups/d.tar.age"
    restore "$backups/d.tar.age"
    check "${case_#*:}, checksum matching: refused" failed
    check "  by age" err_says "age could not decrypt it"
    check "  before any database is created" not_called "CREATE DATABASE"
    check "  nothing it had decrypted is left in the work directory" work_empty
done

reset
package_plain "$backups/p.tar.age"
checksum "$backups/p.tar.age"
restore "$backups/p.tar.age"
check "an unencrypted package named .tar.age is refused" failed
check "  as not an age file" err_says "not an age file"
check "  before any database is created" not_called "CREATE DATABASE"
reset
package_plain "$backups/p.tar"
checksum "$backups/p.tar"
restore "$backups/p.tar"
check "an unencrypted package (.tar, E2) is refused" failed
check "  as not encrypted" err_says "only encrypted packages"
check "  before any database is created" not_called "CREATE DATABASE"
reset
stub_dump >"$backups/old.dump"
checksum "$backups/old.dump"
restore "$backups/old.dump"
check "a bare dump is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

echo "== restore.sh: the package inside"
# Where the last entry's data ends: the end-of-archive blocks follow it.
data_end() { python3 -c 'import sys, tarfile; t = tarfile.open(sys.argv[1]); t.getmembers(); print(t.offset)' "$1"; }
reset
package_plain "$work/whole.tar"
end=$(data_end "$work/whole.tar")
for cut in "$((end / 2)):cut inside its entries" "$end:cut where its end-of-archive blocks begin"; do
    : >"$STUB_LOG"
    head -c "${cut%%:*}" "$work/whole.tar" >"$backups/t.tar.age.plain"
    encrypt "$backups/t.tar.age.plain" "$backups/t.tar.age"
    restore "$backups/t.tar.age"
    check "a package ${cut#*:}, encrypted and with a matching checksum, is refused" failed
    check "  as not a valid package" err_says "not a valid backup package"
    check "  before any database is created" not_called "CREATE DATABASE"
    check "  nothing is left in the work directory" work_empty
done

for kind in no-manifest traversal duplicate component-changed; do
    reset
    craft "$backups/x.tar.age" "$kind"
    restore "$backups/x.tar.age"
    check "a package with $kind (encrypted, matching checksum) is refused" failed
    check "  as not a valid package" err_says "not a valid backup package"
    check "  before any database is created" not_called "CREATE DATABASE"
    check "  nothing is left in the work directory" work_empty
done
check "  nothing was written outside the run directory" test ! -e "$work/outside" -a ! -e "$plain_dir/../outside"

reset
fixture "$backups/x.tar.age" truncated
restore "$backups/x.tar.age"
check "a package whose dump is truncated (manifest matching it) is refused" failed
check "  because the dump does not read back in full" err_says "cannot be read back in full"
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar.age" valid server_version_num=170000
restore "$backups/x.tar.age"
check "a dump of a later PostgreSQL major is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
backup
dump=$(cat "$work/out")
mkdir -p "$work/elsewhere"
mv "$dump" "$dump.sha256" "$work/elsewhere/"
: >"$STUB_LOG"
restore "$work/elsewhere/$(basename "$dump")"
check "a package moved to another directory still checks and restores" succeeded
check "  into a new database" called "CREATE DATABASE restore_"

for differs in ALEMBIC=a3f81c5d2e94 SCHEMA=$(printf 'e%.0s' $(seq 64)) "CONTENT=4 $(printf 'd%.0s' $(seq 64))"; do
    reset
    fixture "$backups/x.tar.age"
    run env "STUB_RESTORED_$differs" "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/one.key"
    check "a restored database whose ${differs%%=*} differs from the manifest fails" failed
    check "  and says what differs" err_says "differs from the manifest"
    check "  and the new database is dropped" called "DROP DATABASE IF EXISTS restore_"
    check "  nothing is left in the work directory" work_empty
done

echo "== restore.sh: arguments"
reset
fixture "$backups/x.tar.age"
restore "$backups/x.tar.age" --keep
check "--keep is accepted" succeeded
check "  the restored database is kept" not_called "DROP DATABASE"
check "  nothing is left in the work directory" work_empty

for args in "--kep" "--keep extra" "keep" "--KEEP"; do
    reset
    fixture "$backups/x.tar.age"
    # shellcheck disable=SC2086 # Split on purpose: the arguments of the case.
    restore "$backups/x.tar.age" $args
    check "restore.sh refuses '$args'" failed
    check "  without creating or dropping any database" not_called "DATABASE"
done
reset
run "$ops/restore.sh" "$release"
check "restore.sh without a package is refused" failed

reset
fixture "$backups/x.tar.age"
STUB_RESTORE_RC=1 restore "$backups/x.tar.age"
check "a failed pg_restore is a failure" failed
check "  and the new database is dropped" called "DROP DATABASE IF EXISTS restore_"
check "  nothing is left in the work directory" work_empty

echo "== one operation at a time"
# hold CALL COMMAND...: starts COMMAND in the background, stopped at the docker call
# whose arguments contain CALL, and waits until it gets there.
hold() {
    local call=$1
    shift
    rm -f "$work"/hold.*
    STUB_HOLD="$work/hold" STUB_HOLD_ON=$call timeout 120 "$@" >"$work/held.out" 2>"$work/held.err" &
    held=$!
    wait_for "$work/hold.started"
}
release_held() {
    touch "$work/hold.release"
    held_rc=0
    wait "$held" || held_rc=$?
}

reset
fixture "$backups/x.tar.age"
hold pg_dump "$ops/backup.sh" "$release"
restore "$backups/x.tar.age"
check "restore.sh is refused while a backup runs" failed
check "  and says another operation is running" err_says "another"
check "  without creating any database" not_called "CREATE DATABASE"
run "$ops/content-sync.sh" "$release" "$work/no-checkout" "$(printf '0%.0s' $(seq 40))"
check "content-sync.sh is refused while a backup runs" failed
check "  because of the lock" err_says "another operation (backup, restore, migration, content sync"
check "  without syncing" not_called "content sync"
release_held
check "  the backup itself completes" test "$held_rc" = 0

reset
fixture "$backups/x.tar.age"
hold "CREATE DATABASE" "$ops/restore.sh" "$release" "$backups/x.tar.age" --identity-file "$keys/one.key"
backup
check "backup.sh is refused while a restore runs" failed
check "  without dumping" not_called "pg_dump -U"
release_held
check "  the restore itself completes" test "$held_rc" = 0

reset
hold "alembic upgrade" env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" \
    "$ops/migrate.sh" "$release"
before=$(count_calls pg_dump)
backup
check "backup.sh is refused while a migration runs" failed
check "  without dumping" test "$(count_calls pg_dump)" = "$before"
fixture "$backups/x.tar.age"
restore "$backups/x.tar.age"
check "restore.sh is refused while a migration runs" failed
release_held
check "  the migration itself completes" test "$held_rc" = 0

reset
flock -n "$LINUXLAB_TEST_LOCK_FILE" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
held=$!
wait_for "$work/hold.started"
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" "$ops/migrate.sh" "$release"
check "migrate.sh is refused while another process holds the lock" failed
check "  before reading or changing the database" not_called alembic
release_held

echo "== the production project's lock is fixed"
reset
fixture "$backups/x.tar.age"
: >"$STUB_LOG"
run env LINUXLAB_PROJECT=linuxlab-prod LINUXLAB_TEST_LOCK_FILE="$work/other.lock" "$ops/backup.sh" "$release"
check "LINUXLAB_TEST_LOCK_FILE is refused with the production project" failed
check "  and says so" err_says "LINUXLAB_TEST_LOCK_FILE is for test projects only"
check "  before any call" test ! -s "$STUB_LOG"
for command in "restore.sh $backups/x.tar.age --identity-file $keys/one.key" "migrate.sh"; do
    : >"$STUB_LOG"
    # shellcheck disable=SC2086 # Split on purpose: the script and its arguments.
    set -- $command
    run env LINUXLAB_PROJECT=linuxlab-prod LINUXLAB_TEST_LOCK_FILE="$work/other.lock" \
        "$ops/$1" "$release" "${@:2}"
    check "  $1 refuses it too, before any call" test "$rc" != 0 -a ! -s "$STUB_LOG"
    check "  and says so" err_says "LINUXLAB_TEST_LOCK_FILE is for test projects only"
done
: >"$STUB_LOG"
run env -u LINUXLAB_TEST_LOCK_FILE LINUXLAB_PROJECT=linuxlab-prod LINUXLAB_TEST_BACKUP_RUN=$fixed \
    "$ops/backup.sh" "$release"
check "LINUXLAB_TEST_BACKUP_RUN is refused with the production project" failed
check "  before any call" test ! -s "$STUB_LOG"

# The production project uses /run/linuxlab-ops.lock. As root (a container), hold it
# from another process and check that a production backup is refused because of it;
# otherwise (CI's user) the backup cannot even open it, and says which file it tried.
reset
canonical=/run/linuxlab-ops.lock
if [ -w /run ]; then
    rm -f "$work"/hold.*
    flock -n "$canonical" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
    held=$!
    wait_for "$work/hold.started"
    run env -u LINUXLAB_TEST_LOCK_FILE LINUXLAB_PROJECT=linuxlab-prod "$ops/backup.sh" "$release"
    check "the production project takes $canonical: refused while another process holds it" failed
    check "  and names it" err_says "is running (lock $canonical)"
    release_held
else
    run env -u LINUXLAB_TEST_LOCK_FILE LINUXLAB_PROJECT=linuxlab-prod "$ops/backup.sh" "$release"
    check "the production project uses $canonical (not writable here: refused, naming it)" \
        err_says "cannot open the lock file $canonical"
fi
check "  without dumping" not_called "pg_dump -U"

reset
rm -f "$work"/hold.*
flock -n "$LINUXLAB_TEST_LOCK_FILE" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
held=$!
wait_for "$work/hold.started"
run env LINUXLAB_LOCK_FILE="$work/other.lock" "$ops/backup.sh" "$release"
check "LINUXLAB_LOCK_FILE (no longer read) does not move the lock" failed
check "  without dumping" not_called "pg_dump -U"
release_held

echo "== migrate.sh"
reset
start=$(date +%s)
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" "$ops/migrate.sh" "$release"
check "migrate.sh runs its own backup under its lock, without deadlock" succeeded
check "  it backed up into an encrypted package" test "$(files 'linuxlab-*-pre-migrate-*.tar.age' | wc -l)" = 1
check "  restore-tested before encryption" called "CREATE DATABASE restore_"
check "  and dropped the test database" called "DROP DATABASE IF EXISTS restore_"
check "  it migrated" called "alembic upgrade head"
check "  nothing unencrypted left: BACKUP_DIR" only_encrypted
check "  nor the work directory" work_empty
check "  in well under the timeout" test $(($(date +%s) - start)) -lt 60
backup
check "  and the lock is free afterwards: backup.sh runs" succeeded
reset
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" \
    "STUB_RESTORED_SCHEMA=$(printf 'e%.0s' $(seq 64))" "$ops/migrate.sh" "$release"
check "a backup that does not restore stops the migration" failed
check "  nothing is migrated" not_called "alembic upgrade head"

echo "== no private key in any output"
check "no private key in anything the scripts printed" not grep -q AGE-SECRET-KEY "$work/all.log"

[ "$failures" -eq 0 ] || { echo "$failures checks failed" >&2; exit 1; }
echo "all backup cases passed"
