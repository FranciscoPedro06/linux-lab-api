#!/usr/bin/env bash
# Reports the state of the backups' copies off the host, from this host's files only:
# the latest receipt and its age, backups without a receipt, copies that failed, and
# the free space of BACKUP_DIR. Changes nothing, takes no lock, reads nothing off the
# host. infra/ops/check-labs.sh reports the same. See
# docs/operations.md#copies-off-the-host.
#
#   infra/ops/backup-status.sh
#
# Settings, from the environment file: BACKUP_DIR, BACKUP_REMOTE,
# BACKUP_MAX_AGE_HOURS (default 26).
#
# Exit status 0 when nothing needs attention, 1 otherwise (for cron).
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

DISK_WARN_PERCENT=85
[ $# = 0 ] || fail "usage: backup-status.sh"
[ -f "$ENV_FILE" ] || fail "environment file not found: $ENV_FILE"
command -v python3 >/dev/null || fail "python3 is required"
dir=$(setting BACKUP_DIR)
dir=${dir:-/var/backups/linuxlab}

findings=$(backup_findings "$dir")
if [ -d "$dir" ]; then
    used=$(df -P -- "$dir" | awk 'NR == 2 {sub("%", "", $5); print $5}')
    [ "$used" -lt "$DISK_WARN_PERCENT" ] || findings+=$'\n'"ATTENTION  disk of $dir is $used% full"
fi
echo "$findings"
attention=$(grep -c '^ATTENTION ' <<<"$findings" || true)
if [ "$attention" -gt 0 ]; then
    echo "$attention items need attention"
    exit 1
fi
echo "ok"
