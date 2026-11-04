#!/usr/bin/env bash
# Backs up the deployment into one package: the database (pg_dump, custom format),
# Caddy's data volume (certificates and their keys) and a manifest of what was
# captured and from what, each component read back in full first. Copies the package
# off the host when BACKUP_COPY_COMMAND is set. Removes no backup: see
# docs/operations.md#backups.
#
#   infra/ops/backup.sh RELEASE_FILE [LABEL]
#
# Settings, from the environment file:
#   BACKUP_DIR            local directory (default /var/backups/linuxlab), mode 0700
#   BACKUP_COPY_COMMAND   run with each new file as its last argument, for example
#                         "rclone copy --config /root/.config/rclone/rclone.conf linuxlab-backups:"
#                         Unset: backups stay on this host only, which is reported.
#
# Writes linuxlab-<time>-<id>-<label>.tar, its layout and manifest described in
# infra/ops/package.py, and linuxlab-<time>-<id>-<label>.tar.sha256 next to it. The
# components are gathered in a .partial directory and the package is written as a
# .partial file; it gets its final name, by a hard link that never replaces an
# existing file, only once it checks and has its checksum, both written to disk.
# This run's partial files are removed when it ends, whatever the reason; those of an
# earlier run that did not finish are reported and left alone.
#
# Needs python3 (3.10 or later) on the host, for infra/ops/package.py.
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
web_sha=$(release_value WEB_SHA)
[[ $API_SHA =~ ^[0-9a-f]{40}$ ]] || fail "the release file has no full API_SHA"
[[ $web_sha =~ ^[0-9a-f]{40}$ ]] || fail "the release file has no full WEB_SHA"
command -v python3 >/dev/null || fail "python3 is required (infra/ops/package.py)"
take_lock

umask 077
mkdir -p "$dir"
chmod 700 "$dir"
# Names unique per run: the time to the second and 64 random bits. publish() still
# refuses to replace any existing file, should two runs ever share a name.
now=$(date -u +%s)
created_at=$(date -u -d "@$now" +%Y-%m-%dT%H:%M:%SZ)
run=${TEST_BACKUP_RUN:-$(date -u -d "@$now" +%Y%m%dT%H%M%SZ)-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')}
[[ $run =~ ^[0-9A-Za-z]+-[0-9a-f]+$ ]] || fail "unexpected backup run name: $run"
package="$dir/linuxlab-$run-$label.tar"
stage="$dir/linuxlab-$run-$label.partial"

# This run's partial files and directory; removed when it ends (the package, once
# published, is no longer under its partial name).
partials=("$package.partial" "$package.sha256.partial")
stage_made=false
# shellcheck disable=SC2317 # Run by the EXIT trap.
remove_partials() {
    rm -f -- "${partials[@]}"
    if [ "$stage_made" = true ]; then rm -rf -- "$stage"; fi
}
trap remove_partials EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stale=$(find "$dir" -mindepth 1 -maxdepth 1 -name '*.partial' | wc -l)
[ "$stale" = 0 ] || note "$stale unfinished file(s) from earlier runs in $dir (*.partial): not backups, left as they are"
mkdir -m 700 -- "$stage"
stage_made=true

# What the manifest records, read before the dump. Nothing changes it until the lock
# is released (see lib.sh). The images first: a release file that does not match them
# stops the backup before the database is touched.
api_label=$(image_revision "$API_IMAGE")
web_label=$(image_revision "$(release_value EDGE_IMAGE)")
[ -z "$api_label" ] || [ "$api_label" = "$API_SHA" ] \
    || fail "$API_IMAGE is labeled as built from $api_label, the release file says $API_SHA"
[ -z "$web_label" ] || [ "$web_label" = "$web_sha" ] \
    || fail "the edge image is labeled as built from $web_label, the release file says $web_sha"
api_running=$(running_image api)
edge_running=$(running_image caddy)
[ -z "$api_running" ] || [ "$api_running" = "$(release_value API_IMAGE_ID)" ] \
    || note "the API running now is not this release's image ($api_running): the manifest records both"
roles=$(database_roles "$database")
# The package carries no PostgreSQL globals (roles, their grants and passwords): they
# are not needed while linuxlab, created by the postgres image, is the only role.
[ "$roles" = "linuxlab:true" ] \
    || fail "roles other than the superuser linuxlab ($roles): the package would need PostgreSQL globals, which it does not carry"
server_version=$(psql_on "$database" -c "SHOW server_version")
server_version_num=$(psql_on "$database" -c "SHOW server_version_num")
pg_dump_version=$(compose exec -T postgres pg_dump --version)
revision=$(alembic_revision "$database")
schema=$(schema_fingerprint "$database")
content=$(content_fingerprint "$database")

note "dumping database $database"
compose exec -T postgres pg_dump -U linuxlab -d "$database" --format=custom >"$stage/postgres.dump" \
    || fail "pg_dump failed; no backup written"
verify_dump "$stage/postgres.dump" || fail "the dump cannot be read back in full; no backup written"
components=(--component "database=$stage/postgres.dump")

note "archiving Caddy's data volume"
volume="${PROJECT}_caddy-data"
caddy_data="no volume $volume"
if docker volume inspect "$volume" >/dev/null 2>&1; then
    # As root: Caddy's private keys are readable by root only. Read-only, no network.
    docker run --rm --network none --user 0 -v "$volume:/data:ro" --entrypoint tar "$API_IMAGE" \
        -czf - -C /data . >"$stage/caddy-data.tar.gz" \
        || fail "archiving Caddy's data failed; no backup written"
    # gzip checks every byte against its CRC; tar must list the archive to its end,
    # and list something (GNU tar accepts less than a block of anything as empty).
    entries=$({ gzip -t "$stage/caddy-data.tar.gz" && tar -tzf "$stage/caddy-data.tar.gz"; } 2>/dev/null) || entries=''
    [ -n "$entries" ] || fail "Caddy's data archive cannot be read back in full; no backup written"
    components+=(--component "caddy-data=$stage/caddy-data.tar.gz")
    caddy_data=''
else
    note "no volume $volume; Caddy data not archived"
fi

note "writing the package"
python3 "$OPS_DIR/package.py" build "$package.partial" "${components[@]}" \
    --fact "backup_id=$run" --fact "created_at=$created_at" --fact "label=$label" \
    --fact "release_file=$(basename -- "$RELEASE")" \
    --fact "api_sha=$API_SHA" --fact "api_image_id=$(release_value API_IMAGE_ID)" \
    --fact "api_image_revision=$api_label" --fact "api_running_image_id=$api_running" \
    --fact "web_sha=$web_sha" --fact "edge_image_id=$(release_value EDGE_IMAGE_ID)" \
    --fact "web_image_revision=$web_label" --fact "edge_running_image_id=$edge_running" \
    --fact "database=$database" --fact "server_version=$server_version" \
    --fact "server_version_num=$server_version_num" --fact "pg_dump_version=$pg_dump_version" \
    --fact "alembic_revision=$revision" --fact "schema_fingerprint=$schema" \
    --fact "content=$content" --fact "roles=$roles" --fact "caddy_data=$caddy_data" \
    || fail "the package could not be written; no backup written"
# Read back as restore.sh will: layout, manifest, each component's size and SHA-256.
python3 "$OPS_DIR/package.py" check "$package.partial" >/dev/null \
    || fail "the package does not check; no backup written"
write_checksum "$package.partial" "$package"
publish "$package.partial" "$package"
rm -rf -- "$stage"
stage_made=false
note "package: $package ($(du -h "$package" | cut -f1))"

status=0
if [ -n "$copy" ]; then
    for file in "$package" "$package.sha256"; do
        # The command comes from the root-owned environment file. It runs without
        # the lock's descriptor (9>&-): a process it leaves behind (an ssh master
        # connection, a daemon) cannot hold the lock after this script ends, while
        # this script keeps holding it until then.
        if ! sh -c "$copy \"\$1\"" copy "$file" 9>&-; then
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
    \( -name 'linuxlab-*.tar' -o -name 'linuxlab-*.dump' -o -name 'caddy-data-*.tar.gz' \) | wc -l) backup file(s) in $dir"
# The only output on standard output: the new package, for infra/ops/migrate.sh.
echo "$package"
exit "$status"
