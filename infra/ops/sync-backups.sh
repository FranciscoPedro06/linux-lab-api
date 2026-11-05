#!/usr/bin/env bash
# Copies off the host the backups of BACKUP_DIR that have no receipt yet, as backup.sh
# does after a backup (infra/ops/remote.sh), without taking a new one: for a backup
# whose copy failed (no connection, the destination down) or was interrupted. See
# docs/operations.md#copies-off-the-host.
#
#   infra/ops/sync-backups.sh
#
# Running it again is safe: an object already there and identical is not uploaded
# again; one there with other content is a conflict, reported and left alone, and the
# backup stays here; a backup with a receipt here is not touched. No backup, here or
# off the host, is removed or replaced.
#
# Settings, from the environment file: BACKUP_DIR, BACKUP_REMOTE (required),
# BACKUP_RCLONE_CONFIG, BACKUP_RETRIES and BACKUP_RETRY_DELAY, as backup.sh. Needs no
# release file, Docker or database.
#
# Exit status 0 when every backup has a receipt, 1 otherwise.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

[ $# = 0 ] || fail "usage: sync-backups.sh"
[ -f "$ENV_FILE" ] || fail "environment file not found: $ENV_FILE"
command -v python3 >/dev/null || fail "python3 is required"
dir=$(setting BACKUP_DIR)
dir=${dir:-/var/backups/linuxlab}
[ -d "$dir" ] || fail "no backup directory $dir"
remote_status=0
remote_init || remote_status=$?
[ "$remote_status" != 2 ] || fail "BACKUP_REMOTE is not set: nowhere to copy backups to"
[ "$remote_status" = 0 ] || exit 1
take_lock
umask 077

# Removed when the script ends, whatever happens: receipts not yet published.
# shellcheck disable=SC2317 # Run by the EXIT trap.
cleanup() {
    find "$dir" -mindepth 1 -maxdepth 1 \( -name 'linuxlab-*.tar.age.receipt.json.partial' \
        -o -name 'linuxlab-*.tar.age.push-failed.partial' \) -exec rm -f -- {} +
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

pending=0 confirmed=0 failed=0
while IFS= read -r file; do
    [ -n "$file" ] || continue
    if [ -e "$file.receipt.json" ]; then
        continue
    fi
    pending=$((pending + 1))
    if push_backup "$file"; then
        confirmed=$((confirmed + 1))
    else
        failed=$((failed + 1))
    fi
done < <(local_backups "$dir")

note "$pending backup(s) without a receipt: $confirmed confirmed off the host now, $failed not"
[ "$failed" = 0 ]
