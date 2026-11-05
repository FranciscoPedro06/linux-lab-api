#!/usr/bin/env bash
# Removes from BACKUP_DIR the backups older than BACKUP_LOCAL_RETENTION_DAYS whose copy
# off the host is confirmed again now. Disabled unless that setting is given: there
# is no default number of days. See docs/operations.md#local-retention.
#
#   infra/ops/prune-backups.sh
#
# A backup is removed here (its .tar.age and .tar.age.sha256; the receipt stays, as
# the record of where the backup is) only when all of these hold:
#   - its name says it was made more than BACKUP_LOCAL_RETENTION_DAYS days ago (a name
#     that does not say when is never removed);
#   - it has a receipt here, and it is not the newest backup that has one: the last
#     backup confirmed off the host always stays here too;
#   - just now, the package, its checksum and its receipt were each read back in full
#     from BACKUP_REMOTE and match the files here. A receipt here is not proof enough
#     on its own: the destination may have lost the object since.
# A backup without a receipt (pending) is never removed. Every candidate is confirmed
# first; one object that cannot be read back (no connection, missing, not readable)
# or does not match suspends the whole run, and nothing is removed until a run
# confirms every candidate. Nothing is ever removed off the host.
#
# Settings, from the environment file: BACKUP_LOCAL_RETENTION_DAYS (empty: disabled),
# BACKUP_DIR, BACKUP_REMOTE (required when enabled), BACKUP_RCLONE_CONFIG.
#
# Exit status 0 when disabled or every candidate was handled, 1 otherwise.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

[ $# = 0 ] || fail "usage: prune-backups.sh"
[ -f "$ENV_FILE" ] || fail "environment file not found: $ENV_FILE"
days=$(setting BACKUP_LOCAL_RETENTION_DAYS)
if [ -z "$days" ]; then
    note "BACKUP_LOCAL_RETENTION_DAYS is not set: local retention is disabled; nothing removed"
    exit 0
fi
[[ $days =~ ^[1-9][0-9]{0,3}$ ]] || fail "BACKUP_LOCAL_RETENTION_DAYS must be a whole number of days from 1 to 9999"
command -v python3 >/dev/null || fail "python3 is required"
dir=$(setting BACKUP_DIR)
dir=${dir:-/var/backups/linuxlab}
[ -d "$dir" ] || fail "no backup directory $dir"
remote_status=0
remote_init || remote_status=$?
[ "$remote_status" != 2 ] || fail "BACKUP_REMOTE is not set: no backup can be confirmed off the host, so none is removed"
[ "$remote_status" = 0 ] || exit 1
take_lock

# made_at FILE: when the backup was made, in seconds since the epoch, from its name
# (linuxlab-<YYYYmmddTHHMMSSZ>-...); status 1 when the name does not say.
made_at() {
    local name stamp
    name=$(basename -- "$1")
    [[ $name =~ ^linuxlab-([0-9]{8})T([0-9]{2})([0-9]{2})([0-9]{2})Z- ]] || return 1
    stamp="${BASH_REMATCH[1]} ${BASH_REMATCH[2]}:${BASH_REMATCH[3]}:${BASH_REMATCH[4]}"
    date -u -d "$stamp" +%s 2>/dev/null
}

# still_off_the_host FILE: status 0 when the package, its checksum and its receipt
# read back in full from BACKUP_REMOTE now and match the files here.
still_off_the_host() {
    local file=$1 name found fetched
    name=$(basename -- "$file")
    verify_checksum "$file" || return 1
    receipt_matches "$file.receipt.json" "$file" || { note "$name: its receipt here does not match it"; return 1; }
    found=$(remote_read "$name") || { note "$name: cannot be read back off the host"; return 1; }
    [ "$found" = "$(local_hash "$file")" ] || { note "$name: the copy off the host differs ($found)"; return 1; }
    found=$(remote_read "$name.sha256") || { note "$name.sha256: cannot be read back off the host"; return 1; }
    [ "$found" = "$(local_hash "$file.sha256")" ] || { note "$name.sha256: the copy off the host differs"; return 1; }
    fetched=$(mktemp)
    if ! remote_fetch "$name.receipt.json" "$fetched" || ! receipt_matches "$fetched" "$file"; then
        rm -f -- "$fetched"
        note "$name.receipt.json: cannot be read back off the host, or does not match"
        return 1
    fi
    rm -f -- "$fetched"
}

now=$(date -u +%s)
limit=$((now - days * 86400))
backups=()
while IFS= read -r file; do
    [ -n "$file" ] && backups+=("$file")
done < <(local_backups "$dir")

# The newest backup with a receipt here stays whatever its age.
newest_confirmed=''
for file in "${backups[@]}"; do
    [ -e "$file.receipt.json" ] && newest_confirmed=$file
done
if [ -z "$newest_confirmed" ]; then
    note "no backup here has a receipt: none is confirmed off the host; nothing removed"
    exit 0
fi

# First every candidate is confirmed off the host; only if all of them are, any is
# removed. A failure suspends the whole removal.
candidates=()
kept_pending=0
for file in "${backups[@]}"; do
    [ "$file" != "$newest_confirmed" ] || continue
    if [ ! -e "$file.receipt.json" ]; then
        kept_pending=$((kept_pending + 1))
        continue
    fi
    made=$(made_at "$file") || continue
    [ "$made" -lt "$limit" ] || continue
    if ! still_off_the_host "$file"; then
        note "local removal suspended: $(basename -- "$file") is not confirmed off the host now; nothing removed"
        exit 1
    fi
    candidates+=("$file")
done
for file in "${candidates[@]}"; do
    rm -f -- "$file" "$file.sha256"
    note "removed $(basename -- "$file") here: confirmed off the host now ($REMOTE); its receipt stays"
done
sync -- "$dir"
note "${#candidates[@]} backup(s) removed here (older than $days days, confirmed off the host now); $kept_pending pending backup(s) kept; $(basename -- "$newest_confirmed") kept as the newest confirmed"
