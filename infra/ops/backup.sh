#!/usr/bin/env bash
# Backs up the deployment into one encrypted package: the database (pg_dump, custom
# format), Caddy's data volume (certificates and their keys) and a manifest of what was
# captured and from what, each component read back in full first, the whole package
# encrypted with age to every recipient of BACKUP_AGE_RECIPIENTS_FILE. When
# BACKUP_REMOTE is set, copies it off the host and reads the copy back in full
# (infra/ops/remote.sh). Removes no backup: see docs/operations.md#backups.
#
#   infra/ops/backup.sh RELEASE_FILE [LABEL] [--restore-test]
#
# --restore-test restores the package, before it is encrypted, into a new database,
# compares it with the manifest and drops it (infra/ops/migrate.sh uses it: the host
# holds no key that could decrypt the package afterwards).
#
# Settings, from the environment file:
#   BACKUP_DIR                  local directory (default /var/backups/linuxlab), 0700
#   BACKUP_AGE_RECIPIENTS_FILE  public keys the package is encrypted to, one per line,
#                               at least two (infra/ops/age.sh); required
#   BACKUP_WORK_DIR             where the unencrypted package exists while it is built
#                               (default /run/linuxlab-backup, a tmpfs; see lib.sh)
#   BACKUP_REMOTE               rclone "remote:bucket/prefix" the backup is copied to.
#                               Unset: backups stay on this host only, which is reported.
#   BACKUP_RCLONE_CONFIG        rclone's configuration file, with the credential: 0600
#                               or 0400, this user's (required with BACKUP_REMOTE)
#   BACKUP_RETRIES              attempts at the copy (default 5)
#   BACKUP_RETRY_DELAY          seconds before the second attempt, doubled before each
#                               one after (default 30)
#
# The copy off the host: the package, its checksum, then a receipt, each uploaded
# without replacing anything there and read back in full (bytes and SHA-256); then
# the receipt here, linuxlab-<...>.tar.age.receipt.json. A copy that cannot be
# confirmed is retried; if it still cannot, the backup stays here, pending, the exit
# status is 1, and infra/ops/sync-backups.sh retries it later without a new dump.
#
# Writes linuxlab-<time>-<id>-<label>.tar.age, the package (infra/ops/package.py)
# encrypted, and linuxlab-<time>-<id>-<label>.tar.age.sha256, the SHA-256 of those
# encrypted bytes. Nothing unencrypted is ever written to BACKUP_DIR: the components
# and the package are in a run directory of BACKUP_WORK_DIR, removed as soon as the
# package is encrypted and again when the run ends (trap). The encrypted file is
# written as .partial and gets its final name, by a hard link that never replaces an
# existing file, only once it has its checksum, both written to disk.
#
# Needs age (infra/release/install-age.sh) and python3 (3.10 or later) on the host.
# Exit status other than 0 if any step failed, so it can be run from cron and alert.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

usage="usage: backup.sh RELEASE_FILE [LABEL] [--restore-test]"
restore_test=false
positional=()
for argument in "$@"; do
    case "$argument" in
        --restore-test) restore_test=true ;;
        -*) fail "unknown argument: $argument ($usage)" ;;
        *) positional+=("$argument") ;;
    esac
done
if [ "${#positional[@]}" -lt 1 ] || [ "${#positional[@]}" -gt 2 ]; then fail "$usage"; fi
load_release "${positional[0]}"
label=${positional[1]:-scheduled}
[[ $label =~ ^[a-z0-9-]+$ ]] || fail "label must be lowercase letters, digits and dashes"
dir=$(setting BACKUP_DIR)
dir=${dir:-/var/backups/linuxlab}
[ -z "$(setting BACKUP_COPY_COMMAND)" ] \
    || fail "BACKUP_COPY_COMMAND is no longer supported: set BACKUP_REMOTE and BACKUP_RCLONE_CONFIG (docs/operations.md#copies-off-the-host)"
remote_status=0
remote_init || remote_status=$?
[ "$remote_status" != 1 ] || exit 1
recipients_file=$(setting BACKUP_AGE_RECIPIENTS_FILE)
database=$(database_name)
web_sha=$(release_value WEB_SHA)
[[ $API_SHA =~ ^[0-9a-f]{40}$ ]] || fail "the release file has no full API_SHA"
[[ $web_sha =~ ^[0-9a-f]{40}$ ]] || fail "the release file has no full WEB_SHA"
command -v python3 >/dev/null || fail "python3 is required (infra/ops/package.py)"
check_age || exit 1
[ -n "$recipients_file" ] || fail "BACKUP_AGE_RECIPIENTS_FILE is not set: no backup without encryption"
recipients=$(age_recipients "$recipients_file") || exit 1
recipient_count=$(wc -l <<<"$recipients")
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
encrypted="$dir/linuxlab-$run-$label.tar.age"

# This run's partial files in BACKUP_DIR (encrypted) and its run directory in
# BACKUP_WORK_DIR (unencrypted); both removed when it ends. A restore test's database
# is dropped too.
partials=("$encrypted.partial" "$encrypted.sha256.partial" "$encrypted.receipt.json.partial" "$encrypted.push-failed.partial")
RUN_DIR=''
# shellcheck disable=SC2317 # Run by the EXIT trap.
cleanup() {
    drop_restore_target
    rm -f -- "${partials[@]}"
    if [ -n "$RUN_DIR" ]; then rm -rf -- "$RUN_DIR"; fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

stale=$(find "$dir" -mindepth 1 -maxdepth 1 -name '*.partial' | wc -l)
[ "$stale" = 0 ] || note "$stale unfinished file(s) from earlier runs in $dir (*.partial): not backups, left as they are"

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

# The dump, Caddy's data and the package exist together in the work directory: twice
# the database's size, plus room for Caddy's data and the restore test's unpacking.
database_bytes=$(psql_on "$database" -c "SELECT pg_database_size(current_database())")
copies=2
if [ "$restore_test" = true ]; then copies=3; fi
needed=$((database_bytes * copies + 64 * 1024 * 1024))
prepare_work "$needed"
stage="$RUN_DIR/stage"
plain="$RUN_DIR/package.tar"
mkdir -m 700 -- "$stage"

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
python3 "$OPS_DIR/package.py" build "$plain" "${components[@]}" \
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
rm -rf -- "$stage"
# Read back as restore.sh will: layout, manifest, each component's size and SHA-256.
if [ "$restore_test" = true ]; then
    python3 "$OPS_DIR/package.py" check "$plain" --extract "$RUN_DIR/unpacked" >"$RUN_DIR/expected" \
        || fail "the package does not check; no backup written"
    verify_dump "$RUN_DIR/unpacked/database/postgres.dump" || fail "the packaged dump does not read back; no backup written"
    # Its report goes to standard error: standard output is the package's path only.
    # Any failure in it ends this script (set -e, fail), and the trap drops the database.
    restore_database "$RUN_DIR/unpacked/database/postgres.dump" "$RUN_DIR/expected" >&2
    drop_restore_target
    RESTORE_TARGET=''
    rm -rf -- "$RUN_DIR/unpacked"
    note "restore test passed"
else
    python3 "$OPS_DIR/package.py" check "$plain" >/dev/null \
        || fail "the package does not check; no backup written"
fi

note "encrypting the package to $recipient_count recipients"
age --encrypt --recipients-file "$recipients_file" --output "$encrypted.partial" "$plain" \
    || fail "age could not encrypt the package; no backup written"
# Nothing unencrypted is kept past this point.
rm -rf -- "$RUN_DIR"
RUN_DIR=''
# One X25519 stanza per recipient in the header: the file is readable with any of them.
stanzas=$(LC_ALL=C sed -n '/^--- /q;p' "$encrypted.partial" | grep -c '^-> X25519 ' || true)
[ "$stanzas" = "$recipient_count" ] \
    || fail "the encrypted package names $stanzas recipients, not $recipient_count; no backup written"
write_checksum "$encrypted.partial" "$encrypted"
publish "$encrypted.partial" "$encrypted"
note "encrypted package: $encrypted ($(du -h "$encrypted" | cut -f1))"

# Off the host, when BACKUP_REMOTE is set: the package, its checksum and a receipt,
# each uploaded without replacing anything and read back in full (infra/ops/remote.sh).
# A failure keeps the backup here, pending, for infra/ops/sync-backups.sh.
status=0
if [ "$remote_status" = 0 ]; then
    push_backup "$encrypted" || status=1
else
    note "BACKUP_REMOTE not set: this backup exists on this host only"
fi

# No backup is ever removed here: infra/ops/prune-backups.sh removes old ones, only
# when it is enabled and only once their copies off the host are read back again.
# The only output on standard output: the new encrypted package, for infra/ops/migrate.sh.
echo "$encrypted"
exit "$status"
