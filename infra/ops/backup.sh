#!/usr/bin/env bash
# Backs up the deployment's database (pg_dump, custom format) and Caddy's data
# volume (certificates and their keys), checks that both can be read back in full,
# and copies them off the host when BACKUP_COPY_COMMAND is set. Removes nothing:
# see docs/operations.md#backups.
#
#   infra/ops/backup.sh RELEASE_FILE [LABEL]
#
# Settings, from the environment file:
#   BACKUP_DIR            local directory (default /var/backups/linuxlab), mode 0700
#   BACKUP_COPY_COMMAND   run with each new file as its last argument, for example
#                         "rclone copy --config /root/.config/rclone/rclone.conf linuxlab-backups:"
#                         Unset: backups stay on this host only, which is reported.
#
# Every file is written as NAME.partial and gets its final name only once it has been
# read back in full and has a checksum (NAME.sha256, naming the file without its
# directory). Partial files of this run are removed when it ends, whatever the
# reason; those of an earlier run that did not finish are reported and left alone.
#
# Exit status other than 0 if any step failed, so it can be run from cron and alert.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

if [ $# -lt 1 ] || [ $# -gt 2 ]; then fail "usage: backup.sh RELEASE_FILE [LABEL]"; fi
load_release "$1"
label=${2:-scheduled}
[[ $label =~ ^[a-z0-9-]+$ ]] || fail "label must be lowercase letters, digits and dashes"
dir=$(setting BACKUP_DIR)
dir=${dir:-/var/backups/linuxlab}
copy=$(setting BACKUP_COPY_COMMAND)
database=$(database_name)
take_lock

umask 077
mkdir -p "$dir"
chmod 700 "$dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dump="$dir/linuxlab-$stamp-$label.dump"
caddy="$dir/caddy-data-$stamp-$label.tar.gz"

# This run's partial files; removed when it ends (renamed ones are gone by then).
partials=()
# shellcheck disable=SC2317 # Run by the EXIT trap.
remove_partials() {
    [ "${#partials[@]}" = 0 ] || rm -f -- "${partials[@]}"
}
trap remove_partials EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stale=$(find "$dir" -maxdepth 1 -type f -name '*.partial' | wc -l)
[ "$stale" = 0 ] || note "$stale unfinished file(s) from earlier runs in $dir (*.partial): not backups, left as they are"

note "dumping database $database"
partials+=("$dump.partial" "$dump.sha256.partial")
compose exec -T postgres pg_dump -U linuxlab -d "$database" --format=custom >"$dump.partial" \
    || fail "pg_dump failed; no backup written"
verify_dump "$dump.partial" || fail "the dump cannot be read back in full; no backup written"
write_checksum "$dump.partial" "$dump"
finish "$dump.partial" "$dump"
note "database: $dump ($(du -h "$dump" | cut -f1))"

note "archiving Caddy's data volume"
volume="${PROJECT}_caddy-data"
if docker volume inspect "$volume" >/dev/null 2>&1; then
    partials+=("$caddy.partial" "$caddy.sha256.partial")
    # As root: Caddy's private keys are readable by root only. Read-only, no network.
    docker run --rm --network none --user 0 -v "$volume:/data:ro" --entrypoint tar "$API_IMAGE" \
        -czf - -C /data . >"$caddy.partial" \
        || fail "archiving Caddy's data failed; the database backup $dump is kept"
    # gzip checks every byte against its CRC; tar must list the archive to its end,
    # and list something (GNU tar accepts less than a block of anything as empty).
    entries=$({ gzip -t "$caddy.partial" && tar -tzf "$caddy.partial"; } 2>/dev/null) || entries=''
    [ -n "$entries" ] \
        || fail "Caddy's data archive cannot be read back in full; the database backup $dump is kept"
    write_checksum "$caddy.partial" "$caddy"
    finish "$caddy.partial" "$caddy"
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
    [ "$status" = 0 ] && note "copy command succeeded for every file (the copies are not verified)"
else
    note "BACKUP_COPY_COMMAND not set: this backup exists on this host only"
fi

# A copy command's exit status does not prove a copy exists off the host, so no
# backup is removed here until copies are verified there.
note "automatic removal of old backups is suspended: $(find "$dir" -maxdepth 1 -type f \
    \( -name 'linuxlab-*.dump' -o -name 'caddy-data-*.tar.gz' \) | wc -l) backup file(s) in $dir"
# The only output on standard output: the new dump, for infra/ops/migrate.sh.
echo "$dump"
exit "$status"
