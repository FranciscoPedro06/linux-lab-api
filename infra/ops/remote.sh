# shellcheck shell=bash disable=SC2034 # RCLONE_VERSION and RCLONE_SHA256 are read by the scripts that source this file.
# Copies of encrypted backups off the host, with rclone, and the proof that they are
# there: every object read back in full and compared, byte count and SHA-256, with the
# file on this host. Sourced by lib.sh, infra/release/check-env.sh and
# infra/release/install-rclone.sh. Prints nothing secret: never the rclone
# configuration or any part of it. See docs/operations.md#copies-off-the-host.

# The release CI installs and tests, and the one infra/release/install-rclone.sh
# installs on the host. RCLONE_SHA256 is the SHA-256 of rclone-<version>-linux-amd64.zip,
# as GitHub publishes it for the release's asset.
RCLONE_VERSION=v1.75.2
RCLONE_SHA256=349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702

# BACKUP_REMOTE, rclone's "remote:bucket/prefix": the name of a remote of the
# configuration file, then a bucket and an optional prefix. Letters, digits, '.', '_',
# '-' and '/' only: no spaces, nothing a shell or rclone would read as an option, a
# pattern or another remote; no "..", no empty path element.
BACKUP_REMOTE_PATTERN='^[A-Za-z0-9_-]+:[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$'

# Status 0 when `rclone` is on the PATH and is the tested release; says why otherwise.
check_rclone() {
    local version
    if ! command -v rclone >/dev/null; then
        echo "error: rclone is not installed (infra/release/install-rclone.sh)" >&2
        return 1
    fi
    version=$(rclone version 2>/dev/null | sed -n '1s/^rclone //p') || version=''
    if [ "$version" != "$RCLONE_VERSION" ]; then
        echo "error: rclone ${version:-unknown} is installed; the scripts are tested with $RCLONE_VERSION (infra/release/install-rclone.sh)" >&2
        return 1
    fi
}

# check_remote_settings REMOTE CONFIG: status 0 when both can be used; says why not.
# CONFIG holds the credential: a private_file (infra/ops/age.sh), as identity files.
check_remote_settings() {
    local remote=$1 config=$2
    if ! [[ $remote =~ $BACKUP_REMOTE_PATTERN ]] || [[ $remote == *..* ]]; then
        echo "error: BACKUP_REMOTE must be remote:bucket[/prefix], with letters, digits, '.', '_', '-' and '/' only, and no '..'" >&2
        return 1
    fi
    if [ -z "$config" ]; then
        echo "error: BACKUP_RCLONE_CONFIG is not set: the rclone configuration file holding the credential" >&2
        return 1
    fi
    private_file "$config" "rclone configuration"
}

# check_retry_settings RETRIES DELAY: status 0 when both are usable; says why not.
check_retry_settings() {
    if ! [[ $1 =~ ^[1-9][0-9]?$ ]]; then
        echo "error: BACKUP_RETRIES must be a whole number from 1 to 99" >&2
        return 1
    fi
    if ! [[ $2 =~ ^[0-9]{1,4}$ ]]; then
        echo "error: BACKUP_RETRY_DELAY must be a number of seconds from 0 to 9999" >&2
        return 1
    fi
}

# Reads standard input to its end and prints its SHA-256 and its size in bytes.
HASH_AND_SIZE='import hashlib, sys
digest, size = hashlib.sha256(), 0
while chunk := sys.stdin.buffer.read(1 << 20):
    digest.update(chunk)
    size += len(chunk)
print(digest.hexdigest(), size)'

# Set by remote_init, used by everything below.
REMOTE=''
REMOTE_CONFIG=''
REMOTE_RETRIES=5
REMOTE_RETRY_DELAY=30

# remote_init: reads BACKUP_REMOTE, BACKUP_RCLONE_CONFIG, BACKUP_RETRIES and
# BACKUP_RETRY_DELAY. Status 0 when they can be used, 1 when not (says why), 2 when
# BACKUP_REMOTE is not set (no copies off the host are configured).
remote_init() {
    REMOTE=$(setting BACKUP_REMOTE)
    REMOTE_CONFIG=$(setting BACKUP_RCLONE_CONFIG)
    REMOTE_RETRIES=$(setting BACKUP_RETRIES)
    REMOTE_RETRIES=${REMOTE_RETRIES:-5}
    REMOTE_RETRY_DELAY=$(setting BACKUP_RETRY_DELAY)
    REMOTE_RETRY_DELAY=${REMOTE_RETRY_DELAY:-30}
    [ -n "$REMOTE" ] || return 2
    check_rclone || return 1
    check_remote_settings "$REMOTE" "$REMOTE_CONFIG" || return 1
    check_retry_settings "$REMOTE_RETRIES" "$REMOTE_RETRY_DELAY" || return 1
}

# rclone itself: its arguments one by one, never through a shell; the configuration
# file named; one attempt per call (push_backup retries); errors only; and without the
# lock's descriptor (9>&-), so that nothing rclone leaves behind can hold the lock.
# --s3-no-check-bucket: the host's credential can neither create nor list buckets.
rclone_run() {
    rclone --config "$REMOTE_CONFIG" --s3-no-check-bucket --retries 1 --low-level-retries 3 \
        --contimeout 15s --timeout 120s --log-level ERROR "$@" 9>&-
}

# remote_read NAME: prints "<SHA-256> <bytes>" of the object NAME under BACKUP_REMOTE,
# read in full as a stream (rclone cat --no-traverse needs GetObject only, no
# listing). Status 1 when it cannot be read: missing, not readable with this
# credential (S3 says the same for both without the right to list), or the connection
# failed. An object that is short or changed reads with status 0: the caller compares.
remote_read() {
    (
        set -o pipefail
        rclone_run cat --no-traverse -- "$REMOTE/$1" 2>/dev/null | python3 -c "$HASH_AND_SIZE"
    )
}

# remote_fetch NAME OUTPUT: writes the object NAME to the file OUTPUT, in full.
remote_fetch() {
    rclone_run cat --no-traverse -- "$REMOTE/$1" 2>/dev/null >"$2"
}

# remote_put FILE NAME: uploads FILE as NAME. The write is conditional (If-None-Match:
# *): the server refuses it when NAME exists, so nothing there is ever replaced. On a
# backend that ignores the condition, see docs/operations.md#copies-off-the-host.
# Always one PUT request (--s3-upload-cutoff 5G, above REMOTE_MAX_BYTES): the
# condition is tested on that request only, not on multipart uploads, and an
# interrupted PUT leaves no object. No multipart upload is ever made.
#
# REMOTE_MAX_BYTES: the largest file uploaded, 5,000,000,000 bytes, within what one
# PUT request may carry on S3 and the providers that follow it (5 GB; some count
# it in decimal units). A larger file is refused before any upload.
REMOTE_MAX_BYTES=5000000000

# remote_size_allowed BYTES: status 0 when a file of BYTES bytes may be uploaded.
remote_size_allowed() {
    [[ $1 =~ ^[0-9]+$ ]] && [ "$1" -le "$REMOTE_MAX_BYTES" ]
}

remote_put() {
    rclone_run copyto --no-check-dest --s3-upload-cutoff 5G --header-upload "If-None-Match: *" \
        -- "$1" "$REMOTE/$2"
}

# local_hash FILE: "<SHA-256> <bytes>" of FILE, the same form as remote_read.
local_hash() {
    python3 -c "$HASH_AND_SIZE" <"$1"
}

# put_verified FILE NAME: makes the object NAME a copy of FILE, proven by reading it
# back in full. Status 0: the object has FILE's bytes (uploaded now, or there from an
# earlier attempt). 1: not confirmed, try again later (no connection, the upload or
# the read failed). 2: an object NAME exists with other content: it is not
# overwritten and is not retried; a person must look.
put_verified() {
    local file=$1 name=$2 expected found
    if ! remote_size_allowed "$(stat -c %s -- "$file")"; then
        note "$name: larger than $REMOTE_MAX_BYTES bytes, the most one upload request takes; not uploaded (docs/operations.md#copies-off-the-host)"
        return 2
    fi
    expected=$(local_hash "$file")
    if found=$(remote_read "$name"); then
        if [ "$found" = "$expected" ]; then
            note "$name: already off the host, read back in full, identical"
            return 0
        fi
        note "$name: CONFLICT: an object of that name off the host has other content (sha256 and bytes: $found; here: $expected); not overwritten"
        return 2
    fi
    if ! remote_put "$file" "$name"; then
        note "$name: upload failed"
        return 1
    fi
    if ! found=$(remote_read "$name"); then
        note "$name: uploaded, but it cannot be read back"
        return 1
    fi
    if [ "$found" != "$expected" ]; then
        note "$name: CONFLICT: read back as $found after the upload, expected $expected"
        return 2
    fi
    note "$name: uploaded and read back in full, identical ($expected)"
}

# write_receipt FILE OUTPUT: writes the receipt of FILE's verified copy to OUTPUT (a
# new file). What it says was verified: both objects read back just before.
write_receipt() {
    python3 - "$1" "$2" "$REMOTE" "$RCLONE_VERSION" <<'PY'
import datetime, hashlib, json, os, sys

path, output, destination, rclone_version = sys.argv[1:5]

def sha256(name):
    digest = hashlib.sha256()
    with open(name, "rb") as source:
        while chunk := source.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()

name = os.path.basename(path)
receipt = {
    "format_version": 1,
    "backup_id": name.removeprefix("linuxlab-").removesuffix(".tar.age"),
    "object": name,
    "size": os.path.getsize(path),
    "sha256": sha256(path),
    "checksum_object": name + ".sha256",
    "checksum_sha256": sha256(path + ".sha256"),
    "destination": destination,
    "rclone_version": rclone_version,
    "verified_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "status": "verified",
}
with open(output, "x", encoding="utf-8") as out:
    json.dump(receipt, out, indent=2)
    out.write("\n")
PY
}

# receipt_matches RECEIPT FILE: status 0 when the file RECEIPT is a receipt of FILE's
# copy at BACKUP_REMOTE: verified, same object name, size and SHA-256, same checksum
# file. Anything else (another backup's, another destination's, not JSON) is not.
receipt_matches() {
    python3 - "$1" "$2" "$REMOTE" <<'PY'
import hashlib, json, os, sys

receipt, path, destination = sys.argv[1:4]

def sha256(name):
    digest = hashlib.sha256()
    with open(name, "rb") as source:
        while chunk := source.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()

try:
    with open(receipt, encoding="utf-8") as source:
        r = json.load(source)
    name = os.path.basename(path)
    ok = (
        isinstance(r, dict)
        and r.get("format_version") == 1
        and r.get("status") == "verified"
        and r.get("object") == name
        and r.get("checksum_object") == name + ".sha256"
        and r.get("destination") == destination
        and r.get("size") == os.path.getsize(path)
        and r.get("sha256") == sha256(path)
        and r.get("checksum_sha256") == sha256(path + ".sha256")
    )
except (OSError, ValueError, UnicodeDecodeError):
    ok = False
sys.exit(0 if ok else 1)
PY
}

# push_attempt FILE: one attempt at confirming FILE (a .tar.age with its .sha256) off
# the host, in this order: the package, then its checksum, each uploaded unless there
# and read back in full; then the receipt, uploaded and read back the same way (or,
# when an earlier attempt left one there that matches, that one); and only then the
# receipt on this host, FILE.receipt.json, published as backups are (written to disk,
# a hard link that never replaces a file, the directory written to disk). Status as
# put_verified. Changes nothing of FILE or FILE.sha256. The receipt is written and
# checked in a temporary directory, outside BACKUP_DIR, until it is published.
push_attempt() {
    local file=$1 name receipt candidate status=0
    name=$(basename -- "$file")
    receipt="$file.receipt.json"
    verify_checksum "$file" || return 2
    put_verified "$file" "$name" || return
    put_verified "$file.sha256" "$name.sha256" || return
    candidate=$(mktemp -d)
    if remote_fetch "$name.receipt.json" "$candidate/receipt.json" \
        && receipt_matches "$candidate/receipt.json" "$file"; then
        note "$name.receipt.json: already off the host, read back, matches this backup"
    else
        rm -f -- "$candidate/receipt.json"
        write_receipt "$file" "$candidate/receipt.json"
        put_verified "$candidate/receipt.json" "$name.receipt.json" || status=$?
    fi
    if [ "$status" = 0 ]; then
        rm -f -- "$receipt.partial"
        cp -- "$candidate/receipt.json" "$receipt.partial"
    fi
    rm -rf -- "$candidate"
    [ "$status" = 0 ] || return "$status"
    sync -- "$receipt.partial"
    if ! ln -T -- "$receipt.partial" "$receipt"; then
        rm -f -- "$receipt.partial"
        note "$receipt exists already; not replaced"
        return 2
    fi
    rm -f -- "$receipt.partial"
    sync -- "$(dirname -- "$receipt")"
}

# push_backup FILE: push_attempt, up to BACKUP_RETRIES times, waiting
# BACKUP_RETRY_DELAY seconds before the second attempt and twice as long before each
# one after. A conflict is not retried. On failure FILE.push-failed records when and
# why (for backup-status.sh) and the status is 1; on success it is removed. FILE is
# never removed or changed here, whatever happens.
push_backup() {
    local file=$1 name attempt=1 delay=$REMOTE_RETRY_DELAY status reason
    name=$(basename -- "$file")
    while true; do
        status=0
        push_attempt "$file" || status=$?
        if [ "$status" = 0 ] || [ "$status" = 2 ] || [ "$attempt" -ge "$REMOTE_RETRIES" ]; then
            break
        fi
        note "$name: attempt $attempt of $REMOTE_RETRIES failed; next in $delay s"
        sleep "$delay"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
    if [ "$status" = 0 ]; then
        rm -f -- "$file.push-failed"
        note "$name: confirmed off the host ($REMOTE)"
        return 0
    fi
    reason="not confirmed off the host after $attempt attempt(s)"
    [ "$status" = 2 ] && reason="conflict off the host or locally; not retried, see the log"
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$reason" >"$file.push-failed.partial"
    mv -f -- "$file.push-failed.partial" "$file.push-failed"
    note "$name: NOT confirmed off the host: $reason; the backup stays here"
    return 1
}

# local_backups DIR: the backups of DIR, one path per line, oldest first (their names
# start with the UTC time they were made).
local_backups() {
    find "$1" -mindepth 1 -maxdepth 1 -type f -name 'linuxlab-*.tar.age' | LC_ALL=C sort
}

# receipt_time RECEIPT: the verified_at of a receipt, as seconds since the epoch;
# status 1 if it has none that can be read.
receipt_time() {
    python3 - "$1" <<'PY'
import datetime, json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as source:
        stamp = json.load(source)["verified_at"]
    when = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(1)
print(int(when.replace(tzinfo=datetime.timezone.utc).timestamp()))
PY
}

# backup_findings DIR: the state of the copies off the host of DIR's backups, read from
# this host's files only, one line each; lines that need a person start with
# "ATTENTION  ". A receipt here says that a copy was verified when it was written,
# not that it is still there (prune-backups.sh reads it back before relying on it).
# Uses BACKUP_REMOTE and BACKUP_MAX_AGE_HOURS (default 26: a daily backup, plus room
# for its retries).
backup_findings() {
    local dir=$1 max_age file latest='' latest_at=0 at pending=0 hours used
    max_age=$(setting BACKUP_MAX_AGE_HOURS)
    max_age=${max_age:-26}
    if ! [[ $max_age =~ ^[1-9][0-9]{0,4}$ ]]; then
        echo "ATTENTION  BACKUP_MAX_AGE_HOURS must be a whole number of hours"
        max_age=26
    fi
    # Receipts are compared with this destination (a receipt of another one does not
    # match).
    REMOTE=$(setting BACKUP_REMOTE)
    if [ -z "$REMOTE" ]; then
        echo "ATTENTION  BACKUP_REMOTE is not set: backups exist on this host only"
    fi
    if [ ! -d "$dir" ]; then
        echo "ATTENTION  no backup directory $dir"
        return 0
    fi
    while IFS= read -r file; do
        [ -n "$file" ] || continue
        [ -e "$file.receipt.json" ] || pending=$((pending + 1))
        if [ -e "$file.push-failed" ]; then
            echo "ATTENTION  $(basename -- "$file"): copy off the host failed: $(head -c 300 -- "$file.push-failed" | tr -d '\n')"
        fi
    done < <(local_backups "$dir")
    [ "$pending" = 0 ] \
        || echo "ATTENTION  $pending backup(s) here without a receipt: not confirmed off the host (infra/ops/sync-backups.sh)"
    while IFS= read -r file; do
        [ -n "$file" ] || continue
        if ! at=$(receipt_time "$file"); then
            echo "ATTENTION  $(basename -- "$file"): not a receipt that can be read"
            continue
        fi
        if [ -e "${file%.receipt.json}" ] && ! receipt_matches "$file" "${file%.receipt.json}"; then
            echo "ATTENTION  $(basename -- "$file"): does not match its backup"
            continue
        fi
        if [ "$at" -gt "$latest_at" ]; then
            latest=$file
            latest_at=$at
        fi
    done < <(find "$dir" -mindepth 1 -maxdepth 1 -type f -name 'linuxlab-*.tar.age.receipt.json' | LC_ALL=C sort)
    if [ -z "$latest" ]; then
        echo "ATTENTION  no backup has a valid receipt: none is confirmed off the host"
    else
        hours=$((($(date -u +%s) - latest_at) / 3600))
        echo "backups: latest confirmed off the host: $(basename -- "${latest%.receipt.json}"), $hours h ago"
        [ "$hours" -lt "$max_age" ] \
            || echo "ATTENTION  the latest copy confirmed off the host is $hours h old (BACKUP_MAX_AGE_HOURS $max_age)"
    fi
    used=$(df -P -- "$dir" | awk 'NR == 2 {sub("%", "", $5); print $5}')
    echo "backups: $(df -Pk -- "$dir" | awk 'NR == 2 {print int($4 / 1024)}') MB free in $dir ($used% used)"
}
