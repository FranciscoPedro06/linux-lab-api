#!/usr/bin/env bash
# Backs up the deployment's database (pg_dump, custom format) and Caddy's data
# volume (certificates and their keys), checks that the dump can be read back,
# copies both off the host when BACKUP_COPY_COMMAND is set, and removes local
# backups older than BACKUP_KEEP_DAYS. See docs/operations.md#backups.
#
#   infra/ops/backup.sh RELEASE_FILE [LABEL]
#
# Settings, from the environment file:
#   BACKUP_DIR            local directory (default /var/backups/linuxlab), mode 0700
#   BACKUP_KEEP_DAYS      local retention in days (default 14)
#   BACKUP_COPY_COMMAND   run with each new file as its last argument, for example
#                         "rclone copy --config /root/.config/rclone/rclone.conf linuxlab-backups:"
#                         Unset: backups stay on this host only, which is reported.
#
# Exit status 1 if any step failed, so it can be run from cron and alert.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

load_release "${1:-}"
label=${2:-scheduled}
[[ $label =~ ^[a-z0-9-]+$ ]] || fail "label must be lowercase letters, digits and dashes"
dir=$(setting BACKUP_DIR)
dir=${dir:-/var/backups/linuxlab}
keep=$(setting BACKUP_KEEP_DAYS)
keep=${keep:-14}
copy=$(setting BACKUP_COPY_COMMAND)
database=$(database_name)

umask 077
mkdir -p "$dir"
chmod 700 "$dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dump="$dir/linuxlab-$stamp-$label.dump"
caddy="$dir/caddy-data-$stamp-$label.tar.gz"

note "dumping database $database"
compose exec -T postgres pg_dump -U linuxlab -d "$database" --format=custom >"$dump.partial"
# A dump that pg_restore cannot list is not a backup.
compose exec -T postgres pg_restore --list <"$dump.partial" >/dev/null \
    || fail "the dump cannot be read back; kept as $dump.partial"
mv "$dump.partial" "$dump"
sha256sum "$dump" >"$dump.sha256"
note "database: $dump ($(du -h "$dump" | cut -f1))"

note "archiving Caddy's data volume"
volume="${PROJECT}_caddy-data"
if docker volume inspect "$volume" >/dev/null 2>&1; then
    # As root: Caddy's private keys are readable by root only. Read-only, no network.
    docker run --rm --network none --user 0 -v "$volume:/data:ro" --entrypoint tar "$API_IMAGE" \
        -czf - -C /data . >"$caddy"
    sha256sum "$caddy" >"$caddy.sha256"
    note "caddy data: $caddy"
else
    note "no volume $volume; Caddy data not archived"
    caddy=''
fi

status=0
if [ -n "$copy" ]; then
    for file in "$dump" "$dump.sha256" ${caddy:+"$caddy" "$caddy.sha256"}; do
        # The command comes from the root-owned environment file.
        if ! sh -c "$copy \"\$1\"" copy "$file"; then
            note "copy off the host FAILED for $file"
            status=1
        fi
    done
    [ "$status" = 0 ] && note "copied off the host"
else
    note "BACKUP_COPY_COMMAND not set: this backup exists on this host only"
fi

find "$dir" -maxdepth 1 -type f \( -name 'linuxlab-*.dump*' -o -name 'caddy-data-*.tar.gz*' \) \
    -mtime +"$keep" -print -delete | sed 's/^/removed old backup: /' >&2
# The only output on standard output: the new dump, for infra/ops/migrate.sh.
echo "$dump"
exit "$status"
