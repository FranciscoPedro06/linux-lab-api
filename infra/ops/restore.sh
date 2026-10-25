#!/usr/bin/env bash
# Restores a dump from infra/ops/backup.sh into a NEW database on the deployment's
# PostgreSQL, never into an existing one, and reports what it holds. The running
# deployment keeps using its own database. See docs/operations.md#restoring.
#
#   infra/ops/restore.sh RELEASE_FILE DUMP [--keep]
#
# Without --keep the new database is dropped afterwards: a restore test. With
# --keep it stays, named restore_<time>, for an emergency switch (DATABASE_NAME).
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

load_release "${1:-}"
dump=${2:?usage: restore.sh RELEASE_FILE DUMP [--keep]}
keep=${3:-}
[ -f "$dump" ] || fail "dump not found: $dump"
if [ -f "$dump.sha256" ]; then
    (cd "$(dirname "$dump")" && sha256sum --check --status "$(basename "$dump").sha256") \
        || fail "$dump does not match its checksum"
fi

current=$(database_name)
target="restore_$(date -u +%Y%m%d%H%M%S)"
[ "$target" != "$current" ] || fail "refusing to restore into the database in use"
exists=$(psql_on postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$target'")
[ "$exists" = 0 ] || fail "database $target already exists"

note "creating database $target"
psql_on postgres -c "CREATE DATABASE $target TEMPLATE template0"
# Whatever fails from here on, the new database does not stay behind.
trap 'psql_on postgres -c "DROP DATABASE IF EXISTS $target" >/dev/null 2>&1 || true' EXIT
note "restoring $dump"
compose exec -T postgres pg_restore -U linuxlab -d "$target" --exit-on-error --no-owner <"$dump" \
    || fail "restore failed"
psql_on "$target" -c "ANALYZE"

# Row counts of the tables that exist (an empty database has none).
report() {
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
echo "restored $target:"
report "$target" | sed 's/^/  /'
echo "in use ($current), for comparison:"
report "$current" | sed 's/^/  /'

if [ "$keep" = --keep ]; then
    trap - EXIT
    echo "kept $target. Labs recorded as unfinished in it may no longer exist, and labs"
    echo "created since the dump are not in it: see docs/operations.md#restoring."
else
    note "restore test passed; dropping $target"
fi
