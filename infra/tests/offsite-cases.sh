#!/usr/bin/env bash
# Cases of the discovery, download and checks of infra/ops/restore-offsite.sh
# (--check-only: no Docker, nothing restored) against a real S3 API: MinIO started
# here on 127.0.0.1 and removed afterwards, the real rclone and age of the tested
# releases, with the credentials of docs/operations.md#disaster-recovery: the host's
# (PutObject and GetObject under one prefix) writes the backups, the recovery's
# (ListBucket restricted to that prefix, and GetObject) finds and reads them. The
# backups are made by the real backup.sh with Docker and PostgreSQL stood in for
# (infra/tests/stubs/docker), as in remote-cases.sh. The rest of the recovery, with
# real Docker and PostgreSQL, is infra/tests/disaster.sh.
#
#   infra/tests/offsite-cases.sh
#
# Needs minio and mc (infra/tests/install-minio.sh), rclone, age and age-keygen,
# flock, timeout and python3. Keys, credentials and data are made for this run only.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
ops="$here/../ops"
for program in minio mc rclone age age-keygen flock python3; do
    command -v "$program" >/dev/null || { echo "error: $program is required" >&2; exit 1; }
done
work=$(mktemp -d)
[ "$(stat -f -c %T /dev/shm)" = tmpfs ] || { echo "error: /dev/shm is not a tmpfs here" >&2; exit 1; }
shm=$(mktemp -d /dev/shm/offsite-cases.XXXXXX)
cleanup() {
    kill "${MINIO_PID:-}" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -rf "$work" "$shm"
}
trap cleanup EXIT
failures=0

# shellcheck source-path=SCRIPTDIR source=minio.sh
source "$here/minio.sh"
minio_start "$work/minio"
mkdir -m 700 "$work/conf"
host_config="$work/conf/host.conf"
recovery_config="$work/conf/recovery.conf"
minio_rclone_config "$host_config" "$MINIO_PORT"
minio_recovery_config "$recovery_config" "$MINIO_PORT"
# A third credential, GetObject only (no listing): --receipt is its way in.
printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject"],"Resource":["arn:aws:s3:::backups/host/*"]}]}' >"$work/reader.json"
reader_secret=$(minio_secret)
mc admin policy create admin backup-reader "$work/reader.json" >/dev/null
mc admin user add admin backup-reader-user "$reader_secret" >/dev/null
mc admin policy attach admin backup-reader --user backup-reader-user >/dev/null
reader_config="$work/conf/reader.conf"
(umask 077; printf '%s\n' "[reader]" "type = s3" "provider = Minio" "access_key_id = backup-reader-user" \
    "secret_access_key = $reader_secret" "endpoint = http://127.0.0.1:$MINIO_PORT" >"$reader_config")

export PATH="$here/stubs:$PATH" STUB_LOG="$work/calls" STUB_CADDY=valid
export LINUXLAB_ENV_FILE="$work/env" LINUXLAB_PROJECT=linuxlab-offsite-cases
export LINUXLAB_TEST_LOCK_FILE="$work/ops.lock"
printf 'Filename\tType\tSize\tUsed\tPriority\n' >"$work/swaps"
export LINUXLAB_TEST_SWAPS="$work/swaps"
: >"$STUB_LOG"
release="$work/release.env"
image_id=sha256:$(printf 'a%.0s' $(seq 64))
printf '%s\n' "API_SHA=0123456789abcdef0123456789abcdef01234567" \
    "WEB_SHA=fedcba9876543210fedcba9876543210fedcba98" \
    "API_IMAGE=api" "API_IMAGE_ID=$image_id" "LAB_IMAGE=lab" "LAB_IMAGE_ID=$image_id" \
    "EDGE_IMAGE=edge" "EDGE_IMAGE_ID=$image_id" >"$release"
keys="$work/keys"
mkdir -m 700 "$keys"
for key in operator recovery stranger; do age-keygen -o "$keys/$key.key" 2>/dev/null; done
printf '%s\n' "$(age-keygen -y "$keys/operator.key")" "$(age-keygen -y "$keys/recovery.key")" >"$keys/recipients.txt"
chmod 600 "$keys/recipients.txt"
backups="$work/backups"
mkdir -p "$backups"
(umask 077; printf '%s\n' "BACKUP_DIR=$backups" "BACKUP_AGE_RECIPIENTS_FILE=$keys/recipients.txt" \
    "BACKUP_WORK_DIR=$shm/plain" "BACKUP_REMOTE=offsite:backups/host" "BACKUP_RCLONE_CONFIG=$host_config" \
    "BACKUP_RETRIES=1" "BACKUP_RETRY_DELAY=0" >"$LINUXLAB_ENV_FILE")
mkdir -m 700 "$shm/plain"

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
        failures=$((failures + 1))
    fi
}
not() { ! "$@"; }
failed() { [ "$rc" != 0 ]; }
succeeded() { [ "$rc" = 0 ]; }
err_says() { grep -q -- "$1" "$work/err"; }
out_says() { grep -q -- "$1" "$work/out"; }
object() { mc cat "admin/backups/host/$1" 2>/dev/null; }
place() { mc pipe "admin/backups/host/$1" >/dev/null; }
drop() { mc rm "admin/backups/host/$1" >/dev/null; }
sum() { sha256sum | cut -c1-64; }

# recover [ARGUMENT...]: restore-offsite.sh --check-only with the recovery credential,
# the recovery key and a fresh download directory, then the arguments given.
recover() {
    rm -rf "$work/recovered"
    : >"$STUB_LOG"
    run "$ops/restore-offsite.sh" --check-only --remote recovery:backups/host \
        --rclone-config "$recovery_config" --identity-file "$keys/recovery.key" \
        --recovery-dir "$work/recovered" "$@"
}
selected() { err_says "selected linuxlab-$1"; }
nothing_decrypted() {
    [ -z "$(find "$shm/plain" -mindepth 1)" ] \
        && [ -z "$(find "$work/recovered" -type f ! -name '*.tar.age' ! -name '*.sha256' ! -name 'receipt.json' 2>/dev/null)" ]
}

echo "== backups confirmed off the host by the host's credential (backup.sh)"
names=()
for label in first second third; do
    run "$ops/backup.sh" "$release" "$label"
    check "backup.sh $label: confirmed off the host" succeeded
    names+=("$(basename "$(cat "$work/out")")")
    sleep 1.1
done
first=${names[0]%.tar.age} second=${names[1]%.tar.age} third=${names[2]%.tar.age}
first=${first#linuxlab-} second=${second#linuxlab-} third=${third#linuxlab-}
# The host is lost: nothing of it is used below, its directory included.
rm -rf "$backups"

echo "== the credentials"
R() { rclone --config "$1" --s3-no-check-bucket --retries 1 --low-level-retries 1 "${@:2}"; }
echo probe >"$work/probe"
check "the recovery credential lists the prefix" test "$(R "$recovery_config" lsf --files-only recovery:backups/host | wc -l)" = 9
check "  it cannot create an object" not R "$recovery_config" copyto --no-check-dest "$work/probe" recovery:backups/host/probe
check "  it cannot delete one" not R "$recovery_config" deletefile "recovery:backups/host/${names[0]}"
check "  it cannot list outside the prefix" not R "$recovery_config" lsf recovery:backups
mc pipe admin/backups/elsewhere/secret.txt <"$work/probe" >/dev/null
check "  it cannot read outside the prefix" not R "$recovery_config" cat --no-traverse recovery:backups/elsewhere/secret.txt
check "  every object is still there" test "$(mc ls admin/backups/host/ | wc -l)" = 9
check "the host's credential cannot list (production keeps its rights)" not R "$host_config" lsf offsite:backups/host

echo "== discovery and checks, with the recovery credential only"
recover
check "the newest confirmed backup is found, downloaded, decrypted and checked" succeeded
check "  it is the third" selected "$third"
check "  it says so" out_says "checked: linuxlab-$third.tar.age ($third) downloads, decrypts and checks"
check "  the content commit is reported unknown, and why" err_says "the content's commit is not recorded in this backup"
check "  nothing decrypted is left, here or in the work directory" nothing_decrypted
check "  the download is the object's bytes" \
    test "$(sum <"$work/recovered/$third/linuxlab-$third.tar.age")" = "$(object "linuxlab-$third.tar.age" | sum)"
check "  no Docker call (--check-only)" test ! -s "$STUB_LOG"
recover --identity-file "$keys/operator.key"
check "the operator's key works as well" succeeded

# The oldest backup confirmed last (sync-backups.sh, later): verified_at decides, not the name.
object "linuxlab-$first.tar.age.receipt.json" \
    | sed 's/"verified_at": "[^"]*"/"verified_at": "2099-01-01T00:00:00Z"/' >"$work/late.json"
object "linuxlab-$first.tar.age.receipt.json" >"$work/first-receipt.json"
place "linuxlab-$first.tar.age.receipt.json" <"$work/late.json"
recover
check "receipts are ordered by verified_at, not by name: the one confirmed last is used" selected "$first"
place "linuxlab-$first.tar.age.receipt.json" <"$work/first-receipt.json"

echo "== receipts that are not confirmations"
bogus="linuxlab-20991231T235959Z-ffffffffffffffff-bogus.tar.age"
printf 'not json' | place "$bogus.receipt.json"
object "linuxlab-$third.tar.age.receipt.json" | sed 's/"verified"/"pending"/' \
    | place "linuxlab-20991231T235958Z-eeeeeeeeeeeeeeee-pending.tar.age.receipt.json"
object "linuxlab-$third.tar.age.receipt.json" | sed 's#offsite:backups/host#offsite:backups/other#' \
    | place "linuxlab-$third.tar.age.receipt.json.copy"
printf 'x' | place "notes.txt"
recover
check "a newer receipt that is not JSON, one pending, other objects: ignored" succeeded
check "  the newest valid one is still used" selected "$third"
check "  and the refusals are reported" err_says "refused $bogus.receipt.json: not JSON"
drop "$bogus.receipt.json"
drop "linuxlab-20991231T235958Z-eeeeeeeeeeeeeeee-pending.tar.age.receipt.json"

echo "== objects that do not read back as their receipt says"
object "linuxlab-$third.tar.age" >"$work/third.tar.age"
{ head -c 100 "$work/third.tar.age"; printf 'X'; tail -c +102 "$work/third.tar.age"; } | place "linuxlab-$third.tar.age"
recover
check "the newest package changed off the host: rejected, the next one used" selected "$second"
check "  with a warning naming it" err_says "WARNING: 1 newer backup(s)"
check "  and why" err_says "REJECTED linuxlab-$third.tar.age: the package read back is not the one the receipt describes"
check "  nothing of the rejected package is kept" test ! -e "$work/recovered/$third/linuxlab-$third.tar.age"
head -c 1000 "$work/third.tar.age" | place "linuxlab-$third.tar.age"
recover
check "the newest package cut off the host: rejected, the next one used" selected "$second"
drop "linuxlab-$third.tar.age"
recover
check "the newest package missing off the host: rejected, the next one used" selected "$second"
# rclone cat reads a missing object as an empty directory: zero bytes, status 0.
check "  as zero bytes, not the receipt's" err_says "the package read back is not the one the receipt describes"
place "linuxlab-$third.tar.age" <"$work/third.tar.age"
object "linuxlab-$third.tar.age.sha256" >"$work/third.sha256"
sed 's/^./0/' "$work/third.sha256" | place "linuxlab-$third.tar.age.sha256"
recover
check "the newest checksum object changed: rejected, the next one used" selected "$second"
check "  says so" err_says "its checksum object does not match the receipt"
place "linuxlab-$third.tar.age.sha256" <"$work/third.sha256"
recover
check "once repaired, the newest one is used again" selected "$third"

echo "== a package under another backup's name and receipt"
# The second backup's bytes under a new name, with a checksum and a receipt made for
# them: everything off the host agrees, but the manifest inside is the second's.
fake="20991230T000000Z-dddddddddddddddd-third"
object "linuxlab-$second.tar.age" | place "linuxlab-$fake.tar.age"
digest=$(object "linuxlab-$second.tar.age" | sum)
printf '%s  %s\n' "$digest" "linuxlab-$fake.tar.age" >"$work/fake.sha256"
place "linuxlab-$fake.tar.age.sha256" <"$work/fake.sha256"
python3 - "$work/fake.sha256" "$fake" "$digest" "$(object "linuxlab-$second.tar.age" | wc -c)" >"$work/fake.json" <<'PY'
import hashlib, json, sys
sums, fake, digest, size = sys.argv[1:5]
name = f"linuxlab-{fake}.tar.age"
print(json.dumps({"format_version": 1, "backup_id": fake, "object": name, "size": int(size),
    "sha256": digest, "checksum_object": name + ".sha256",
    "checksum_sha256": hashlib.sha256(open(sums, "rb").read()).hexdigest(),
    "destination": "offsite:backups/host", "rclone_version": "v1.75.2",
    "verified_at": "2099-01-02T00:00:00Z", "status": "verified"}, indent=2))
PY
place "linuxlab-$fake.tar.age.receipt.json" <"$work/fake.json"
recover
check "refused: the manifest inside is of another backup than the receipt" failed
check "  says so" err_says "the package's manifest is of backup ${second%-*}"
check "  nothing decrypted is left" nothing_decrypted
drop "linuxlab-$fake.tar.age.receipt.json"

echo "== without the right to list: --receipt"
recover --rclone-config "$reader_config" --remote reader:backups/host
check "a credential that cannot list is refused without --receipt" failed
check "  and told about --receipt" err_says "give --receipt NAME"
recover --rclone-config "$reader_config" --remote reader:backups/host --receipt "linuxlab-$second.tar.age.receipt.json"
check "with --receipt, that backup is found, downloaded and checked" succeeded
check "  it is the one named" selected "$second"
recover --receipt "linuxlab-$second.tar.age"
check "--receipt that is not a receipt's name is refused" failed

echo "== refused before anything is downloaded or decrypted"
recover --rclone-config "$host_config" --remote offsite:backups/host
check "the host's own credential (BACKUP_RCLONE_CONFIG)" failed
check "  says so" err_says "is the host's own credential"
cp "$host_config" "$work/conf/copy.conf"
recover --rclone-config "$work/conf/copy.conf" --remote offsite:backups/host
check "  a copy of it, too" err_says "is the host's own credential"
recover --remote 'recovery:backups/../host'
check "a remote with '..'" failed
rm -rf "$work/recovered"
run "$ops/restore-offsite.sh" --check-only --remote recovery:backups/host --rclone-config "$recovery_config" \
    --identity-file "$keys/stranger.key" --recovery-dir "$work/recovered"
check "a key that is not a recipient: found and downloaded, not decrypted" failed
check "  says so" err_says "age could not decrypt"
check "  nothing decrypted is left" nothing_decrypted
chmod 644 "$keys/recovery.key"
recover
check "an identity file readable by others" failed
check "  says why" err_says "has mode 644"
chmod 600 "$keys/recovery.key"
printf '%s\n' "Filename Type Size Used Priority" "/swapfile file 4194300 0 -2" >"$work/swaps"
recover
check "unencrypted swap: refused before decrypting" failed
check "  says why" err_says "swap in use and not encrypted: /swapfile"
check "  nothing decrypted" nothing_decrypted
printf 'Filename\tType\tSize\tUsed\tPriority\n' >"$work/swaps"
sed -i "s#^BACKUP_WORK_DIR=.*#BACKUP_WORK_DIR=$work/disk#" "$LINUXLAB_ENV_FILE"
recover
check "a work directory on disk: refused" err_says "not in memory"
sed -i "s#^BACKUP_WORK_DIR=.*#BACKUP_WORK_DIR=$shm/plain#" "$LINUXLAB_ENV_FILE"
chmod 644 "$LINUXLAB_ENV_FILE"
recover
check "an environment file readable by others: refused" err_says "has mode 644"
chmod 600 "$LINUXLAB_ENV_FILE"
run env LINUXLAB_PROJECT=linuxlab-prod "$ops/restore-offsite.sh" --check-only --remote recovery:backups/host \
    --rclone-config "$recovery_config" --identity-file "$keys/recovery.key" --recovery-dir "$work/recovered"
check "the test variables with the production project: refused" err_says "is for test projects only"
recover --extra
check "an unknown argument" err_says "unknown argument: --extra"
run "$ops/restore-offsite.sh" --remote recovery:backups/host --rclone-config "$recovery_config" \
    --identity-file "$keys/recovery.key" --recovery-dir "$work/recovered"
check "without --check-only, the repositories are required" err_says "--api-repo and --web-repo are required"

echo "== nothing confirmed off the host"
recover --remote recovery:backups/host/empty
check "an empty prefix: refused" err_says "no receipt under"
for name in "${names[@]}"; do drop "$name"; done
recover
check "receipts whose packages are all gone: refused" err_says "no backup under recovery:backups/host reads back"
check "  each one reported" test "$(grep -c REJECTED "$work/err")" = 3

if grep -q AGE-SECRET-KEY "$work/all.log"; then
    echo "FAIL  a private key appeared in the output" >&2
    failures=$((failures + 1))
fi
if [ "$failures" -gt 0 ]; then
    echo "$failures offsite cases failed" >&2
    exit 1
fi
echo "all offsite cases passed"
