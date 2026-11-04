# shellcheck shell=bash disable=SC2034 # Variables set here are used by the scripts.
# Shared by the operation scripts in this directory; sourced, not run.
#
# Every script takes the release file written by infra/release/build.sh and works on
# one Compose project:
#
#   LINUXLAB_ENV_FILE    settings and secrets (default /etc/linuxlab/production.env)
#   LINUXLAB_PROJECT     Compose project (default linuxlab-prod)
#   LINUXLAB_OVERRIDES   extra Compose files, separated by ":" (an entry other than
#                        the default one, or the end-to-end test's override)
#
# Only for a project other than linuxlab-prod (the tests' own projects), and refused
# with linuxlab-prod before anything is done:
#
#   LINUXLAB_TEST_LOCK_FILE    lock file instead of /run/linuxlab-ops.lock
#   LINUXLAB_TEST_BACKUP_RUN   the "<time>-<id>" part of backup names, instead of a
#                              new one per run (to make two runs collide)
#
# Nothing here prints a secret.

set -euo pipefail
# Git Bash on Windows rewrites arguments that look like paths: container paths
# must reach docker as written, and host paths go through host_path.
docker() { MSYS_NO_PATHCONV=1 command docker "$@"; }

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "$OPS_DIR/.." && pwd)
# shellcheck source-path=SCRIPTDIR source=age.sh
source "$OPS_DIR/age.sh"
ENV_FILE=${LINUXLAB_ENV_FILE:-/etc/linuxlab/production.env}
PROJECT=${LINUXLAB_PROJECT:-linuxlab-prod}

fail() {
    echo "error: $*" >&2
    exit 1
}

note() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >&2
}

# The production project always uses the fixed lock file, so that no setting can
# give two operations on it different locks. Tests run on projects of their own,
# which do not share the production containers, volumes or database.
PRODUCTION_PROJECT=linuxlab-prod
LOCK_FILE=/run/linuxlab-ops.lock
TEST_BACKUP_RUN=''
if [ "$PROJECT" = "$PRODUCTION_PROJECT" ]; then
    for name in LINUXLAB_TEST_LOCK_FILE LINUXLAB_TEST_BACKUP_RUN; do
        [ -z "${!name:-}" ] || fail "$name is for test projects only; refusing it with project $PROJECT"
    done
else
    LOCK_FILE=${LINUXLAB_TEST_LOCK_FILE:-$LOCK_FILE}
    TEST_BACKUP_RUN=${LINUXLAB_TEST_BACKUP_RUN:-}
fi

# Git Bash on Windows passes paths to docker in Windows form; elsewhere unchanged.
host_path() {
    if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi
}

# The last value of NAME in FILE, as docker compose reads it (no shell expansion).
file_value() {
    sed -n "s/^$2=//p" "$1" | tail -n 1
}

setting() {
    file_value "$ENV_FILE" "$1"
}

release_value() {
    file_value "$RELEASE" "$1"
}

# Loads the release file and refuses to go on unless every tag it names still
# points to the image id recorded when the release was built and checked.
load_release() {
    RELEASE=${1:?the release file is required}
    [ -f "$RELEASE" ] || fail "release file not found: $RELEASE"
    [ -f "$ENV_FILE" ] || fail "environment file not found: $ENV_FILE"
    local name image recorded actual
    for name in API LAB EDGE; do
        image=$(release_value "${name}_IMAGE")
        recorded=$(release_value "${name}_IMAGE_ID")
        actual=$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)
        if [ -z "$image" ] || [ "$actual" != "$recorded" ]; then
            fail "$image is not the image recorded in $RELEASE (rebuild or use the right release)"
        fi
    done
    API_IMAGE=$(release_value API_IMAGE)
    API_SHA=$(release_value API_SHA)
    COMPOSE_ARGS=(-p "$PROJECT" -f "$(host_path "$INFRA_DIR/compose.prod.yml")")
    local override
    IFS=: read -r -a overrides <<<"${LINUXLAB_OVERRIDES:-}"
    for override in "${overrides[@]}"; do
        [ -n "$override" ] && COMPOSE_ARGS+=(-f "$(host_path "$override")")
    done
    COMPOSE_ARGS+=(--env-file "$(host_path "$RELEASE")" --env-file "$(host_path "$ENV_FILE")")
}

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

# psql in the PostgreSQL container, unaligned and quiet, on the given database.
psql_on() {
    local database=$1
    shift
    compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -At -U linuxlab -d "$database" "$@"
}

database_name() {
    local name
    name=$(setting DATABASE_NAME)
    echo "${name:-linuxlab}"
}

# Facts about a database, for the backup's manifest and for restore.sh to compare
# with what it restored. None of them changes while the lock is held: migrations and
# content syncs take it too, and the API changes neither the schema nor the missions.

# Status 0 when the table exists in the public schema.
has_table() {
    [ "$(psql_on "$1" -c "SELECT to_regclass('public.$2') IS NOT NULL")" = t ]
}

# The Alembic revision (empty for a database never migrated).
alembic_revision() {
    if has_table "$1" alembic_version; then
        psql_on "$1" -c "SELECT coalesce(string_agg(version_num, ','), '') FROM alembic_version"
    fi
}

# SHA-256 of the schema: columns, constraints, indexes, triggers, functions and
# sequences of the public schema, without owners, positions or sequence values, so
# that a database restored with --no-owner has the same one.
schema_fingerprint() {
    psql_on "$1" -f - <<'SQL'
-- linuxlab: schema fingerprint
SELECT encode(sha256(convert_to(coalesce(string_agg(item, E'\n' ORDER BY item), ''), 'UTF8')), 'hex')
FROM (
    SELECT format('column %s.%s %s %s %s %s', table_name, column_name, data_type,
                  is_nullable, coalesce(column_default, ''), is_identity) AS item
    FROM information_schema.columns WHERE table_schema = 'public'
    UNION ALL
    SELECT format('constraint %s %s %s', conrelid::regclass, conname, pg_get_constraintdef(oid))
    FROM pg_constraint WHERE connamespace = 'public'::regnamespace
    UNION ALL
    SELECT format('index %s', indexdef) FROM pg_indexes WHERE schemaname = 'public'
    UNION ALL
    SELECT format('trigger %s', pg_get_triggerdef(t.oid))
    FROM pg_trigger AS t JOIN pg_class AS c ON c.oid = t.tgrelid
    WHERE NOT t.tgisinternal AND c.relnamespace = 'public'::regnamespace
    UNION ALL
    SELECT format('function %s %s', oid::regprocedure, md5(prosrc))
    FROM pg_proc WHERE pronamespace = 'public'::regnamespace
    UNION ALL
    SELECT format('sequence %s %s', sequence_name, data_type)
    FROM information_schema.sequences WHERE sequence_schema = 'public'
) AS items
SQL
}

# "<missions> <SHA-256>" of the missions as synced: slug, status, current version and
# its content_hash (empty when the database has no missions table yet).
content_fingerprint() {
    has_table "$1" missions || return 0
    psql_on "$1" -f - <<'SQL'
-- linuxlab: content fingerprint
SELECT count(*) || ' ' || encode(sha256(convert_to(coalesce(string_agg(
           format('%s %s %s %s', m.slug, m.status, m.current_version, v.content_hash),
           E'\n' ORDER BY m.slug), ''), 'UTF8')), 'hex')
FROM missions AS m
JOIN mission_versions AS v ON v.mission_id = m.id AND v.version = m.current_version
SQL
}

# "name:superuser" of every role that is not built in, comma separated.
database_roles() {
    psql_on "$1" -c "SELECT string_agg(rolname || ':' || rolsuper, ',' ORDER BY rolname) FROM pg_roles WHERE rolname !~ '^pg_'"
}

# The commit an image says it was built from (empty when it has no such label).
image_revision() {
    local revision
    revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1")
    [ "$revision" = "<no value>" ] || echo "$revision"
}

# The image id of the service's running container (empty when it is not running).
running_image() {
    local container
    container=$(compose ps -q "$1")
    [ -z "$container" ] || docker inspect --format '{{.Image}}' "$container"
}

# One operation at a time: backup.sh, restore.sh, migrate.sh and content-sync.sh take
# the same lock, without waiting, and stop when another one holds it. The lock is held on file
# descriptor 9, which the scripts migrate.sh starts (backup.sh, restore.sh) inherit:
# flock on that same open file succeeds at once, so they run under their parent's
# lock instead of waiting for it. Run on their own, they open the file and take it.
# The lock file is /run/linuxlab-ops.lock (see LOCK_FILE above).
take_lock() {
    command -v flock >/dev/null || fail "flock (util-linux) is required"
    if ! { [ -e /dev/fd/9 ] && [ /dev/fd/9 -ef "$LOCK_FILE" ]; }; then
        exec 9>>"$LOCK_FILE" || fail "cannot open the lock file $LOCK_FILE"
    fi
    flock -n 9 || fail "another backup, restore, migration or content sync is running (lock $LOCK_FILE); nothing was done"
}

# The SHA-256 of FILE, recorded under NAME (the file's final name, without its
# directory) in NAME.sha256 next to it: `sha256sum --check` works wherever both are.
#   write_checksum FILE FINAL_PATH  writes FINAL_PATH.sha256.partial
write_checksum() {
    local digest
    digest=$(sha256sum <"$1" | cut -c1-64)
    printf '%s  %s\n' "$digest" "$(basename -- "$2")" >"$2.sha256.partial"
}

# Status 0 when FILE.sha256 exists, is a single line naming FILE (by its base name)
# and matches FILE's content. Says why otherwise.
verify_checksum() {
    local file=$1 sums=$1.sha256 line digest
    [ -f "$sums" ] || { echo "error: no checksum: $sums not found" >&2; return 1; }
    [ "$(wc -l <"$sums")" = 1 ] || { echo "error: $sums is not a single checksum line" >&2; return 1; }
    line=$(cat "$sums")
    if ! [[ $line =~ ^([0-9a-f]{64})\ \ (.+)$ ]] || [ "${BASH_REMATCH[2]}" != "$(basename -- "$file")" ]; then
        echo "error: $sums does not name $(basename -- "$file")" >&2
        return 1
    fi
    digest=$(sha256sum <"$file" | cut -c1-64)
    [ "$digest" = "${BASH_REMATCH[1]}" ] || { echo "error: $file does not match its checksum" >&2; return 1; }
}

# Reads a custom-format dump in full with pg_restore, which fails on a dump that is
# truncated or damaged anywhere (`pg_restore --list` reads only its table of contents).
verify_dump() {
    compose exec -T postgres pg_restore -f /dev/null <"$1"
}

# Gives PARTIAL and FINAL.sha256.partial their final names, FINAL and FINAL.sha256,
# without ever replacing an existing file, and so that both survive a crash once
# this returns:
#   - their content is written to disk first (sync of the two files);
#   - each name is a hard link (link(2)), which fails if the name exists, in one
#     step, with no window between a check and the rename; the checksum first, so
#     that a file under its final name always has one;
#   - if the second link fails, the checksum just linked is removed, and only if
#     it is still this run's file (same inode as its partial);
#   - then the partial names are removed and the directory is written to disk
#     (sync of the directory), which makes the new names durable.
# Fails (exit 1) if a final name exists or any step fails; nothing that existed
# before is changed.
publish() {
    local partial=$1 final=$2
    sync -- "$partial" "$final.sha256.partial" || fail "could not write $partial to disk"
    ln -T -- "$final.sha256.partial" "$final.sha256" \
        || fail "could not publish $final.sha256 (an existing file is never replaced)"
    if ! ln -T -- "$partial" "$final"; then
        if [ "$final.sha256" -ef "$final.sha256.partial" ]; then
            rm -f -- "$final.sha256"
        fi
        fail "could not publish $final (an existing file is never replaced)"
    fi
    rm -f -- "$partial" "$final.sha256.partial"
    sync -- "$(dirname -- "$final")" || fail "could not write the directory of $final to disk"
}

# Where unencrypted packages exist while backup.sh builds one or restore.sh unpacks
# one: BACKUP_WORK_DIR, by default /run/linuxlab-backup, on /run's tmpfs (memory,
# gone at reboot). It must be a directory of this user, mode 0700, and not a link;
# it is made so when missing, never chosen in its place. Each run works in a
# directory of its own in it, run.<random>, removed when the run ends (trap). Run
# directories left by a run that was killed are removed by the next backup or
# restore, which holds the lock, so none of them can still be in use.
work_root() {
    local root
    root=$(setting BACKUP_WORK_DIR)
    echo "${root:-/run/linuxlab-backup}"
}

# prepare_work NEEDED_BYTES: checks the work directory and its free space, removes
# what killed runs left in it, and sets RUN_DIR to a new run directory. Needs the lock.
prepare_work() {
    local root needed=$1 free stale
    root=$(work_root)
    [[ $root == /* ]] || fail "BACKUP_WORK_DIR must be an absolute path: $root"
    if [ ! -e "$root" ]; then
        mkdir -m 700 -- "$root" || fail "cannot create the work directory $root"
    fi
    if [ ! -d "$root" ] || [ -L "$root" ]; then fail "the work directory $root is not a directory"; fi
    [ "$(stat -c %u "$root")" = "$(id -u)" ] || fail "the work directory $root is not owned by $(id -un)"
    [ "$(stat -c %a "$root")" = 700 ] || fail "the work directory $root has mode $(stat -c %a "$root"), not 700"
    stale=$(find "$root" -mindepth 1 -maxdepth 1 -name 'run.*' | wc -l)
    if [ "$stale" != 0 ]; then
        find "$root" -mindepth 1 -maxdepth 1 -name 'run.*' -exec rm -rf -- {} +
        note "removed $stale run director(ies) left in $root by runs that were killed"
    fi
    free=$(($(df -Pk -- "$root" | awk 'NR == 2 {print $4}') * 1024))
    [ "$free" -ge "$needed" ] \
        || fail "not enough room in the work directory $root: $free bytes free, about $needed needed; nothing was written (BACKUP_WORK_DIR, docs/operations.md#encryption)"
    RUN_DIR=$(mktemp -d -- "$root/run.XXXXXXXX")
}

# Restoring a package's dump into a new database, checked against its manifest.
# RESTORE_TARGET is the database being restored, for drop_restore_target in the
# caller's EXIT trap; RESTORE_KEEP=true leaves it in place.
RESTORE_TARGET=''
RESTORE_KEEP=false
# shellcheck disable=SC2317 # Run by the callers' EXIT traps.
drop_restore_target() {
    if [ -n "$RESTORE_TARGET" ] && [ "$RESTORE_KEEP" != true ]; then
        psql_on postgres -c "DROP DATABASE IF EXISTS $RESTORE_TARGET" >/dev/null 2>&1 || true
    fi
}

# restore_database DUMP EXPECTED: EXPECTED is what `package.py check` printed for the
# package. Fails (exit 1) on any difference; the caller's trap drops the database.
restore_database() {
    local dump=$1 expected_file=$2 current major target exists revision content
    expected() { sed -n "s/^$1 //p" "$expected_file"; }
    current=$(database_name)
    major=$(($(psql_on postgres -c "SHOW server_version_num") / 10000))
    [ "$major" -ge "$(expected postgresql_major_at_least)" ] \
        || fail "the package needs PostgreSQL $(expected postgresql_major_at_least) or later, the server is $major"
    target="restore_$(date -u +%Y%m%d%H%M%S)"
    [ "$target" != "$current" ] || fail "refusing to restore into the database in use"
    exists=$(psql_on postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$target'")
    [ "$exists" = 0 ] || fail "database $target already exists"

    note "creating database $target"
    psql_on postgres -c "CREATE DATABASE $target TEMPLATE template0"
    RESTORE_TARGET=$target
    note "restoring the package's dump"
    compose exec -T postgres pg_restore -U linuxlab -d "$target" --exit-on-error --no-owner <"$dump" \
        || fail "restore failed"
    psql_on "$target" -c "ANALYZE"

    # The restored database against the manifest: what neither the API nor anything
    # outside the lock changes.
    compare() {
        [ "$2" = "$3" ] || fail "the restored database differs from the manifest: $1 is ${2:--}, the manifest says ${3:--}"
    }
    revision=$(alembic_revision "$target")
    compare "the Alembic revision" "${revision:--}" "$(expected alembic_revision)"
    compare "the schema fingerprint" "$(schema_fingerprint "$target")" "$(expected schema_fingerprint)"
    content=$(content_fingerprint "$target")
    compare "the missions" "${content:--}" "$(expected content)"
    note "the restored database matches the manifest (revision, schema, missions)"

    echo "restored $target:"
    restore_report "$target" | sed 's/^/  /'
    echo "in use ($current), for comparison:"
    restore_report "$current" | sed 's/^/  /'
}

# Row counts of the tables that exist (an empty database has none).
restore_report() {
    psql_on "$1" -c "
        SELECT name || ' ' || (xpath('/row/n/text()', query_to_xml(
                   format('SELECT %s AS n FROM %I', expression, name), false, true, '')))[1]::text
        FROM (VALUES ('alembic_version', 'string_agg(version_num, '','')'),
                     ('users', 'count(*)'), ('lab_sessions', 'count(*)'),
                     ('mission_versions', 'count(*)'), ('user_missions', 'count(*)'),
                     ('validation_runs', 'count(*)'), ('command_history', 'count(*)'))
             AS t (name, expression)
        WHERE to_regclass(name) IS NOT NULL"
    psql_on "$1" -c "SELECT 'tables ' || count(*) FROM pg_tables WHERE schemaname = 'public'"
}
