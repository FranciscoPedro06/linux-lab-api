#!/usr/bin/env bash
# Restores the database of a package from infra/ops/backup.sh into a NEW database on
# the deployment's PostgreSQL, never into an existing one, checks it against the
# package's manifest and reports what it holds. The running deployment keeps using
# its own database. See docs/operations.md#restoring.
#
#   infra/ops/restore.sh RELEASE_FILE PACKAGE [--keep]
#
# Without --keep the new database is dropped afterwards: a restore test. With
# --keep it stays, named restore_<time>, for an emergency switch (DATABASE_NAME).
# Any other argument is refused before anything is done.
#
# Before any database is created: the package's checksum file must be next to it
# (PACKAGE.sha256, as backup.sh writes it) and match; the package must check with
# infra/ops/package.py (layout, manifest, each component's size and SHA-256); the
# dump must read back in full; and the server must be of the dump's major version or
# later. After the restore, the Alembic revision, the schema and the missions must be
# those the manifest recorded. Caddy's data is checked with the package, not restored
# (docs/operations.md#restoring).
#
# Needs python3 (3.10 or later) on the host, for infra/ops/package.py.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

usage="usage: restore.sh RELEASE_FILE PACKAGE [--keep]"
if [ $# -lt 2 ] || [ $# -gt 3 ]; then fail "$usage"; fi
keep=false
if [ $# = 3 ]; then
    [ "$3" = --keep ] || fail "unknown argument: $3 ($usage)"
    keep=true
fi
package=$2
load_release "$1"
command -v python3 >/dev/null || fail "python3 is required (infra/ops/package.py)"
take_lock
[ -f "$package" ] || fail "package not found: $package"
verify_checksum "$package" || fail "refusing $package: its checksum is missing or does not match"

# The package is unpacked into a directory of this run, removed when it ends.
work=$(mktemp -d "${TMPDIR:-/tmp}/linuxlab-restore.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
python3 "$OPS_DIR/package.py" check "$package" --extract "$work/package" >"$work/expected" \
    || fail "refusing $package: it is not a valid backup package"
expected() { sed -n "s/^$1 //p" "$work/expected"; }
dump="$work/package/$(expected database_path)"
verify_dump "$dump" || fail "refusing $package: its dump cannot be read back in full"

current=$(database_name)
major=$(($(psql_on postgres -c "SHOW server_version_num") / 10000))
[ "$major" -ge "$(expected postgresql_major_at_least)" ] \
    || fail "refusing $package: it needs PostgreSQL $(expected postgresql_major_at_least) or later, the server is $major"
target="restore_$(date -u +%Y%m%d%H%M%S)"
[ "$target" != "$current" ] || fail "refusing to restore into the database in use"
exists=$(psql_on postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$target'")
[ "$exists" = 0 ] || fail "database $target already exists"

note "creating database $target"
psql_on postgres -c "CREATE DATABASE $target TEMPLATE template0"
# Whatever fails from here on, the new database does not stay behind.
trap 'psql_on postgres -c "DROP DATABASE IF EXISTS $target" >/dev/null 2>&1 || true; rm -rf -- "$work"' EXIT
note "restoring $package"
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

if [ "$keep" = true ]; then
    trap 'rm -rf -- "$work"' EXIT
    echo "kept $target. Labs recorded as unfinished in it may no longer exist, and labs"
    echo "created since the dump are not in it: see docs/operations.md#restoring."
else
    note "restore test passed; dropping $target"
fi
