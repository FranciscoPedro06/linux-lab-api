#!/usr/bin/env bash
# Cases of infra/ops/backup.sh, restore.sh, migrate.sh and content-sync.sh with the
# docker CLI replaced by infra/tests/stubs/docker: no container or database is
# involved, and everything is written under a temporary directory. Packages are
# built and checked by the real infra/ops/package.py, tar, gzip and sha256sum; only
# PostgreSQL and Docker are stand-ins (infra/tests/e2e.sh runs the real ones). Each
# case checks what the script did (the files it left, the calls it made), not only
# its exit status.
#
#   infra/tests/backup-cases.sh
#
# Needs flock (util-linux), gzip, tar, timeout and python3.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
ops="$here/../ops"
work=$(mktemp -d)
cleanup() {
    touch "$work/hold.release"
    wait
    rm -rf "$work"
}
trap cleanup EXIT
export PATH="$here/stubs:$PATH" STUB_LOG="$work/calls"
export LINUXLAB_ENV_FILE="$work/env" LINUXLAB_PROJECT=linuxlab-backup-cases
export LINUXLAB_TEST_LOCK_FILE="$work/ops.lock"
backups="$work/backups"
release="$work/release.env"
api_sha=0123456789abcdef0123456789abcdef01234567
web_sha=fedcba9876543210fedcba9876543210fedcba98
image_id=sha256:$(printf 'a%.0s' $(seq 64))
printf '%s\n' "API_SHA=$api_sha" "WEB_SHA=$web_sha" \
    "API_IMAGE=api" "API_IMAGE_ID=$image_id" "LAB_IMAGE=lab" "LAB_IMAGE_ID=$image_id" \
    "EDGE_IMAGE=edge" "EDGE_IMAGE_ID=$image_id" >"$release"
HEAD='e6b40c7d9a18 (head)\n'
failures=0

# A fresh backup directory, call log and environment file (extra settings as args).
reset() {
    rm -rf "$backups" "$work/elsewhere" "$work/offsite" "$work"/hold.*
    mkdir -p "$backups"
    : >"$STUB_LOG"
    printf '%s\n' "BACKUP_DIR=$backups" "$@" >"$LINUXLAB_ENV_FILE"
}

# run COMMAND...: exit status in $rc, standard output in $work/out, errors in $work/err.
run() {
    rc=0
    timeout 120 "$@" >"$work/out" 2>"$work/err" || rc=$?
}

check() {
    local name=$1
    shift
    if "$@"; then
        echo "ok    $name"
    else
        echo "FAIL  $name" >&2
        sed 's/^/      out: /' "$work/out" >&2
        sed 's/^/      err: /' "$work/err" >&2
        failures=$((failures + 1))
    fi
}

not() { ! "$@"; }
failed() { [ "$rc" != 0 ]; }
succeeded() { [ "$rc" = 0 ]; }
called() { grep -q -- "$1" "$STUB_LOG"; }
not_called() { ! grep -q -- "$1" "$STUB_LOG"; }
count_calls() { grep -c -- "$1" "$STUB_LOG" || true; }
files() { find "$backups" -maxdepth 1 -type f -name "$1" | sort; }
none() { [ -z "$(files "$1")" ]; }
one() { [ "$(files "$1" | wc -l)" = 1 ]; }
# No partial file or directory, of any run.
no_partials() { [ -z "$(find "$backups" -mindepth 1 -maxdepth 1 -name '*.partial')" ]; }
err_says() { grep -q -- "$1" "$work/err"; }

# wait_for FILE: up to 30 s.
wait_for() {
    for _ in $(seq 300); do
        [ -e "$1" ] && return 0
        sleep 0.1
    done
    echo "timed out waiting for $1" >&2
    return 1
}

# The stub's dump (see stubs/docker): intact, or missing its end.
dump_data='rows of every table'
stub_dump() {
    if [ "${1:-valid}" = truncated ]; then
        printf 'PGDMP-STUB\n%s\n' "$dump_data"
    else
        printf 'PGDMP-STUB\n%s\nEND %s\n' "$dump_data" "$(printf '%s' "$dump_data" | sha256sum | cut -c1-64)"
    fi
}

checksum() {
    printf '%s  %s\n' "$(sha256sum <"$1" | cut -c1-64)" "$(basename "$1")" >"$1.sha256"
}

# fixture PACKAGE [DUMP] [FACT=VALUE...]: a package built by package.py, as backup.sh
# builds them, from the stub's dump, with the facts the stub answers (or those given),
# and its checksum.
fixture() {
    local file=$1 mode=${2:-valid} stage
    shift $(($# > 1 ? 2 : 1))
    stage=$(mktemp -d "$work/fixture.XXXXXX")
    stub_dump "$mode" >"$stage/postgres.dump"
    local facts=(
        backup_id=20260101T031500Z-0123456789abcdef created_at=2026-01-01T03:15:00Z
        label=scheduled release_file=release.env "api_sha=$api_sha" "api_image_id=$image_id"
        api_image_revision= api_running_image_id= "web_sha=$web_sha" "edge_image_id=$image_id"
        web_image_revision= edge_running_image_id= database=linuxlab server_version=16.15
        server_version_num=160015 "pg_dump_version=pg_dump (PostgreSQL) 16.15"
        alembic_revision=e6b40c7d9a18 "schema_fingerprint=$(printf 'c%.0s' $(seq 64))"
        "content=5 $(printf 'd%.0s' $(seq 64))" roles=linuxlab:true "caddy_data=no volume"
    )
    local given
    for given in "$@"; do
        facts=("${facts[@]/#${given%%=*}=*/$given}")
    done
    mkdir -p "$(dirname "$file")"
    python3 "$ops/package.py" build "$file" --component "database=$stage/postgres.dump" \
        "${facts[@]/#/--fact=}"
    rm -rf "$stage"
    checksum "$file"
}

# craft PACKAGE KIND: a package that package.py would never build, with a matching
# checksum, so that only the package's own checks can refuse it.
craft() {
    fixture "$1"
    python3 - "$1" "$2" <<'PY'
import io, sys, tarfile
path, kind = sys.argv[1], sys.argv[2]
with tarfile.open(path) as tar:
    entries = [(m, tar.extractfile(m).read()) for m in tar.getmembers()]
def add(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
    for member, data in entries:
        if kind == "no-manifest" and member.name == "manifest.json":
            continue
        if kind == "component-changed" and member.name == "database/postgres.dump":
            data = data.replace(b"rows", b"rowz")
        add(tar, member.name, data)
    if kind == "traversal":
        add(tar, "../outside", b"x")
    if kind == "duplicate":
        add(tar, "database/postgres.dump", entries[1][1])
PY
    checksum "$1"
}

manifest() { tar -xOf "$1" manifest.json; }
# json PACKAGE EXPRESSION: a value of the package's manifest, as JSON.
json() { manifest "$1" | python3 -c "import json, sys; m = json.load(sys.stdin); print(json.dumps($2))"; }

backup() { run "$ops/backup.sh" "$release" "$@"; }
restore() { run "$ops/restore.sh" "$release" "$@"; }
schema_c=$(printf 'c%.0s' $(seq 64))

echo "== backup.sh: one package"
reset
backup
package=$(cat "$work/out")
check "a backup writes one package" succeeded
check "  stdout is its path, and only that" test -f "$package" -a "$(wc -l <"$work/out")" = 1
check "  named linuxlab-<time>-<id>-<label>.tar" \
    grep -Eq "^$backups/linuxlab-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}-scheduled\.tar$" "$work/out"
check "  with its checksum, naming the file, not a path" \
    grep -Eq '^[0-9a-f]{64}  linuxlab-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}-scheduled\.tar$' "$package.sha256"
check "  and nothing else in the directory" test "$(files '*' | wc -l)" = 2
check "  no partial file or directory is left" no_partials
check "  it holds exactly manifest.json and the dump, as regular files" \
    test "$(tar -tvf "$package" | awk '{print substr($1, 1, 1) " " $NF}' | sort | tr '\n' ,)" \
    = "- database/postgres.dump,- manifest.json,"
check "  the dump in it is the one pg_dump wrote" \
    test "$(tar -xOf "$package" database/postgres.dump)" = "$(stub_dump)"
check "  it checks with package.py" python3 "$ops/package.py" check "$package"
check "  its manifest is valid JSON" python3 -c "import json, sys; json.load(sys.stdin)" < <(manifest "$package")
check "  format_version 1" test "$(json "$package" 'm["format_version"]')" = 1
check "  the API and web commits of the release file" \
    test "$(json "$package" '[m["code"]["api"]["sha"], m["code"]["web"]["sha"]]')" = "[\"$api_sha\", \"$web_sha\"]"
check "  no content commit, and why" \
    test "$(json "$package" 'm["code"]["content"]["sha"] is None and "not recorded" in m["code"]["content"]["source"]')" = true
check "  the missions' fingerprint" test "$(json "$package" 'm["code"]["content"]["missions"]')" = 5
check "  the database, its server, pg_dump and Alembic revision" \
    test "$(json "$package" '[m["postgresql"][k] for k in ("database", "server_version_num", "pg_dump_version", "alembic_revision", "schema_fingerprint")]')" \
    = "[\"linuxlab\", 160015, \"pg_dump (PostgreSQL) 16.15\", \"e6b40c7d9a18\", \"$schema_c\"]"
check "  the dump's size and SHA-256" \
    test "$(json "$package" '[(c["path"], c["size"], c["sha256"]) for c in m["components"]]')" \
    = "[[\"database/postgres.dump\", $(stub_dump | wc -c), \"$(stub_dump | sha256sum | cut -c1-64)\"]]"
check "  Caddy's data absent, and why" test "$(json "$package" 'm["caddy"]["data"]')" = "\"absent: no volume linuxlab-backup-cases_caddy-data\""
check "  the package was written to disk before its name existed" \
    grep -Eq '^sync .*/linuxlab-[^ ]+\.tar\.partial \(final absent\)$' "$STUB_LOG"
check "  and the directory after it existed" grep -Eq "^sync $backups \(final files: 2\)$" "$STUB_LOG"

for mode in truncated corrupt fail; do
    reset
    STUB_DUMP=$mode backup
    check "a $mode dump is rejected" failed
    check "  no package is presented as a backup" none 'linuxlab-*.tar*'
    check "  no partial file or directory is left" no_partials
    check "  nothing is printed as a backup" test ! -s "$work/out"
done

echo "== backup.sh: Caddy's data"
reset
STUB_CADDY=valid backup
package=$(cat "$work/out")
check "an intact Caddy archive is packaged" succeeded
check "  as caddy/caddy-data.tar.gz" tar -tf "$package" caddy/caddy-data.tar.gz
caddy_lists() {
    tar -xOf "$package" caddy/caddy-data.tar.gz | tar -tzf - >"$work/caddy.list" && grep -q site.key "$work/caddy.list"
}
check "  which lists in full" caddy_lists
check "  the manifest says so" test "$(json "$package" 'm["caddy"]["data"]')" = '"included"'
check "  and why Caddy's config is not there" \
    test "$(json "$package" '"autosave.json" in m["caddy"]["config"]')" = true
for mode in truncated corrupt flipped fail; do
    reset
    STUB_CADDY=$mode backup
    check "a $mode Caddy archive is rejected" failed
    check "  no package is presented as a backup" none 'linuxlab-*.tar*'
    check "  no partial file or directory is left" no_partials
done

echo "== backup.sh: what the manifest records"
reset
STUB_ROLES=app:false,linuxlab:true backup
check "a database with roles other than linuxlab is refused (no globals in the package)" failed
check "  before any dump" not_called "pg_dump -U"
reset
STUB_REVISION_api=1111111111111111111111111111111111111111 backup
check "an API image labeled with another commit than the release file's is refused" failed
check "  before any dump" not_called "pg_dump -U"
reset
STUB_REVISION_edge=1111111111111111111111111111111111111111 backup
check "an edge image labeled with another commit than the release file's is refused" failed
reset
running=sha256:$(printf 'b%.0s' $(seq 64))
STUB_REVISION_api=$api_sha STUB_REVISION_edge=$web_sha STUB_RUNNING_api=$running STUB_RUNNING_caddy=$image_id backup
package=$(cat "$work/out")
check "matching labels and running containers are recorded" \
    test "$(json "$package" '[m["code"]["api"]["image_revision_label"], m["code"]["web"]["image_revision_label"], m["code"]["api"]["running_image_id"], m["code"]["web"]["running_image_id"]]')" \
    = "[\"$api_sha\", \"$web_sha\", \"$running\", \"$image_id\"]"
check "  a running API that is not the release's is reported" err_says "not this release's image"
reset
STUB_TABLES=f backup
package=$(cat "$work/out")
check "a database never migrated: no Alembic revision and no missions" \
    test "$(json "$package" '[m["postgresql"]["alembic_revision"], m["code"]["content"]["fingerprint"]]')" = "[null, null]"

echo "== backup.sh: copies off the host and retention"
reset "BACKUP_COPY_COMMAND=false"
fixture "$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.tar"
touch -d '30 days ago' "$backups"/linuxlab-20260101T031500Z-*
backup
check "a failed copy off the host is a failure" failed
check "  the failure is reported" err_says "FAILED"
check "  the old backup and its checksum are still there" \
    test "$(files 'linuxlab-20260101T031500Z-*' | wc -l)" = 2
check "  the new package is kept too" test "$(files 'linuxlab-*.tar' | wc -l)" = 2

reset "BACKUP_COPY_COMMAND=cp -t $work/offsite"
mkdir -p "$work/offsite"
fixture "$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.tar"
touch -d '30 days ago' "$backups"/linuxlab-20260101T031500Z-*
backup
check "a successful copy is a success" succeeded
check "  the package and its checksum were copied" test "$(find "$work/offsite" -type f | wc -l)" = 2
check "  an old backup is not removed on the copy command's word alone" \
    test "$(files 'linuxlab-20260101T031500Z-*' | wc -l)" = 2
check "  the removal is reported as suspended" err_says "suspended"

reset
backup
check "without BACKUP_COPY_COMMAND the backup succeeds" succeeded
check "  and says it is on this host only" err_says "BACKUP_COPY_COMMAND not set"

echo "== backup.sh: partial files, interruptions and arguments"
reset
stale="$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.tar.partial"
echo "an earlier run that never finished" >"$stale"
mkdir "$backups/linuxlab-20260101T031500Z-0123456789abcdef-scheduled.partial"
backup
check "partial files of an earlier run are not backups" test "$(cat "$work/out")" != "$stale"
check "  and are left as they are" test -f "$stale" -a -d "${stale%.tar.partial}.partial"
check "  and are reported" err_says "2 unfinished"

reset
backup Not-A-Label
check "an invalid label is refused" failed
check "  before any dump" not_called "pg_dump -U"
reset
backup scheduled extra
check "an extra argument is refused" failed
check "  before any dump" not_called "pg_dump -U"

for at in pg_dump "-czf -"; do
    reset
    STUB_CADDY=valid STUB_HOLD="$work/hold" STUB_HOLD_ON=$at timeout 120 "$ops/backup.sh" "$release" \
        >"$work/out" 2>"$work/err" &
    pid=$!
    wait_for "$work/hold.started"
    kill -TERM "$pid"
    touch "$work/hold.release"
    rc=0
    wait "$pid" || rc=$?
    check "a backup interrupted during '$at' fails" failed
    check "  and leaves no package and no partial file or directory" \
        test -z "$(find "$backups" -mindepth 1)"
done

reset
STUB_HOLD="$work/hold" STUB_HOLD_ON=pg_dump "$ops/backup.sh" "$release" >/dev/null 2>&1 &
pid=$!
wait_for "$work/hold.started"
kill -KILL "$pid"
wait "$pid" 2>/dev/null || true
touch "$work/hold.release"
sleep 0.5
check "a backup killed outright (no trap runs) leaves only a partial directory" \
    test -z "$(files 'linuxlab-*.tar*')" -a -n "$(find "$backups" -maxdepth 1 -type d -name '*.partial')"

echo "== backup.sh: names are never reused or replaced"
reset
backup
first=$(cat "$work/out")
backup
second=$(cat "$work/out")
check "two backups run back to back, same label: two names" \
    test -n "$first" -a -n "$second" -a "$first" != "$second"
check "  both kept" test "$(files 'linuxlab-*.tar' | wc -l)" = 2

sums() { sha256sum "$@" 2>&1; }
reset
fixed=20260101T031500Z-0123456789abcdef
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a backup under a given name" succeeded
mapfile -t earlier < <(files "*-$fixed-*")
before=$(sums "${earlier[@]}")
: >"$STUB_LOG"
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a second run under the same name fails" failed
check "  and says it replaces nothing" err_says "never replaced"
check "  every file of the earlier backup is unchanged" test "$(sums "${earlier[@]}")" = "$before"
check "  no file was added" test "$(files '*' | wc -l)" = 2
check "  no partial file or directory is left" no_partials
restore "$backups/linuxlab-$fixed-scheduled.tar"
check "  the earlier backup still restores" succeeded

reset
taken="$backups/linuxlab-$fixed-scheduled.tar"
echo "a file that happens to have the package's name" >"$taken"
before=$(sums "$taken")
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a run whose package name is taken after its checksum was published fails" failed
check "  the existing file is unchanged" test "$(sums "$taken")" = "$before"
check "  the checksum it had just published is withdrawn" test ! -e "$taken.sha256"
check "  no partial file or directory is left" no_partials
check "  nothing is printed as a backup" test ! -s "$work/out"

reset
fixture "$backups/linuxlab-20260101T000000Z-aaaaaaaaaaaaaaaa-scheduled.tar"
mapfile -t valid < <(files 'linuxlab-*')
before=$(sums "${valid[@]}")
echo "taken" >"$taken"
LINUXLAB_TEST_BACKUP_RUN=$fixed backup
check "a failed publication leaves an earlier valid backup as it was" test "$(sums "${valid[@]}")" = "$before"
restore "$backups/linuxlab-20260101T000000Z-aaaaaaaaaaaaaaaa-scheduled.tar"
check "  and it still restores" succeeded

reset
run env LINUXLAB_TEST_BACKUP_RUN='../../etc-x' "$ops/backup.sh" "$release"
check "a run name with a path in it is refused" failed
check "  before any dump" not_called "pg_dump -U"

echo "== restore.sh: the package and its checksum"
reset
fixture "$backups/x.tar"
restore "$backups/x.tar"
check "an intact package with its checksum restores" succeeded
check "  into a new database" called "CREATE DATABASE restore_"
check "  compared with the manifest" err_says "matches the manifest"
check "  which is dropped afterwards" called "DROP DATABASE IF EXISTS restore_"

reset
fixture "$backups/x.tar"
rm "$backups/x.tar.sha256"
restore "$backups/x.tar"
check "a package without its checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar"
echo "tampered" >>"$backups/x.tar"
restore "$backups/x.tar"
check "a package changed after its checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar"
sed -i 's/[0-9a-f]\{64\}/'"$(printf '0%.0s' $(seq 64))"'/' "$backups/x.tar.sha256"
restore "$backups/x.tar"
check "a changed checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar"
sed -i 's/  x\.tar$/  other.tar/' "$backups/x.tar.sha256"
restore "$backups/x.tar"
check "a checksum naming another file is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

# Where the last entry's data ends: the end-of-archive blocks follow it.
data_end() { python3 -c 'import sys, tarfile; t = tarfile.open(sys.argv[1]); t.getmembers(); print(t.offset)' "$1"; }
reset
fixture "$backups/x.tar"
end=$(data_end "$backups/x.tar")
for cut in "$((end / 2)):cut inside its entries" "$end:cut where its end-of-archive blocks begin"; do
    : >"$STUB_LOG"
    head -c "${cut%%:*}" "$backups/x.tar" >"$backups/t.tar"
    checksum "$backups/t.tar"
    restore "$backups/t.tar"
    check "a package ${cut#*:}, with a matching checksum, is refused" failed
    check "  as not a valid package" err_says "not a valid backup package"
    check "  before any database is created" not_called "CREATE DATABASE"
done

for kind in no-manifest traversal duplicate component-changed; do
    reset
    craft "$backups/x.tar" "$kind"
    restore "$backups/x.tar"
    check "a package with $kind (matching checksum) is refused" failed
    check "  as not a valid package" err_says "not a valid backup package"
    check "  before any database is created" not_called "CREATE DATABASE"
done
check "  nothing was written outside the restore's directory" test ! -e "$work/outside" -a ! -e "$backups/../outside"

reset
fixture "$backups/x.tar" truncated
restore "$backups/x.tar"
check "a package whose dump is truncated (manifest matching it) is refused" failed
check "  because the dump does not read back in full" err_says "cannot be read back in full"
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.tar" valid server_version_num=170000
restore "$backups/x.tar"
check "a dump of a later PostgreSQL major is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
mkdir -p "$backups"
stub_dump >"$backups/old.dump"
checksum "$backups/old.dump"
restore "$backups/old.dump"
check "a bare dump (not a package) is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
backup
dump=$(cat "$work/out")
mkdir -p "$work/elsewhere"
mv "$dump" "$dump.sha256" "$work/elsewhere/"
: >"$STUB_LOG"
restore "$work/elsewhere/$(basename "$dump")"
check "a package moved to another directory still checks and restores" succeeded
check "  into a new database" called "CREATE DATABASE restore_"

for differs in ALEMBIC=a3f81c5d2e94 SCHEMA=$(printf 'e%.0s' $(seq 64)) "CONTENT=4 $(printf 'd%.0s' $(seq 64))"; do
    reset
    fixture "$backups/x.tar"
    run env "STUB_RESTORED_$differs" "$ops/restore.sh" "$release" "$backups/x.tar"
    check "a restored database whose ${differs%%=*} differs from the manifest fails" failed
    check "  and says what differs" err_says "differs from the manifest"
    check "  and the new database is dropped" called "DROP DATABASE IF EXISTS restore_"
done

echo "== restore.sh: arguments"
reset
fixture "$backups/x.tar"
restore "$backups/x.tar" --keep
check "--keep is accepted" succeeded
check "  the restored database is kept" not_called "DROP DATABASE"

for args in "--kep" "--keep extra" "keep" "--KEEP"; do
    reset
    fixture "$backups/x.tar"
    # shellcheck disable=SC2086 # Split on purpose: the arguments of the case.
    restore "$backups/x.tar" $args
    check "restore.sh refuses '$args'" failed
    check "  without creating or dropping any database" not_called "DATABASE"
done
reset
restore
check "restore.sh without a package is refused" failed

reset
fixture "$backups/x.tar"
STUB_RESTORE_RC=1 restore "$backups/x.tar"
check "a failed pg_restore is a failure" failed
check "  and the new database is dropped" called "DROP DATABASE IF EXISTS restore_"

echo "== one operation at a time"
# hold CALL COMMAND...: starts COMMAND in the background, stopped at the docker call
# whose arguments contain CALL, and waits until it gets there.
hold() {
    local call=$1
    shift
    rm -f "$work"/hold.*
    STUB_HOLD="$work/hold" STUB_HOLD_ON=$call timeout 120 "$@" >"$work/held.out" 2>"$work/held.err" &
    held=$!
    wait_for "$work/hold.started"
}
release_held() {
    touch "$work/hold.release"
    held_rc=0
    wait "$held" || held_rc=$?
}

reset
fixture "$backups/x.tar"
hold pg_dump "$ops/backup.sh" "$release"
restore "$backups/x.tar"
check "restore.sh is refused while a backup runs" failed
check "  and says another operation is running" err_says "another"
check "  without creating any database" not_called "CREATE DATABASE"
run "$ops/content-sync.sh" "$release" "$work/no-checkout" "$(printf '0%.0s' $(seq 40))"
check "content-sync.sh is refused while a backup runs" failed
check "  because of the lock" err_says "another backup, restore, migration or content sync"
check "  without syncing" not_called "content sync"
release_held
check "  the backup itself completes" test "$held_rc" = 0

reset
fixture "$backups/x.tar"
hold "CREATE DATABASE" "$ops/restore.sh" "$release" "$backups/x.tar"
backup
check "backup.sh is refused while a restore runs" failed
check "  without dumping" not_called "pg_dump -U"
release_held
check "  the restore itself completes" test "$held_rc" = 0

reset
hold "alembic upgrade" env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" \
    "$ops/migrate.sh" "$release"
before=$(count_calls pg_dump)
backup
check "backup.sh is refused while a migration runs" failed
check "  without dumping" test "$(count_calls pg_dump)" = "$before"
fixture "$backups/x.tar"
restore "$backups/x.tar"
check "restore.sh is refused while a migration runs" failed
release_held
check "  the migration itself completes" test "$held_rc" = 0

reset
flock -n "$LINUXLAB_TEST_LOCK_FILE" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
held=$!
wait_for "$work/hold.started"
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" "$ops/migrate.sh" "$release"
check "migrate.sh is refused while another process holds the lock" failed
check "  before reading or changing the database" not_called alembic
release_held

echo "== the copy command does not get the lock"
# Stands for a copy command that leaves a process behind (an ssh master connection,
# a daemon): the first call starts a background process, then waits to be released.
cat >"$work/copy.sh" <<'EOF'
#!/usr/bin/env bash
if [ ! -e "$COPY_DIR/copy.started" ]; then
    sleep 60 &
    echo $! >"$COPY_DIR/copy.background"
    touch "$COPY_DIR/copy.started"
    while [ ! -e "$COPY_DIR/copy.release" ]; do sleep 0.1; done
fi
EOF
chmod +x "$work/copy.sh"
reset "BACKUP_COPY_COMMAND=$work/copy.sh"
rm -f "$work/copy.started" "$work/copy.release" "$work/copy.background"
COPY_DIR=$work timeout 120 "$ops/backup.sh" "$release" >"$work/held.out" 2>"$work/held.err" &
held=$!
wait_for "$work/copy.started"
fixture "$backups/x.tar"
restore "$backups/x.tar"
check "while the copy command runs, the backup still holds the lock" failed
check "  restore.sh was refused without creating a database" not_called "CREATE DATABASE"
touch "$work/copy.release"
held_rc=0
wait "$held" || held_rc=$?
check "the backup ends" test "$held_rc" = 0
background=$(cat "$work/copy.background")
check "  the copy command's background process is still running" kill -0 "$background"
holds_lock() {
    local fd
    for fd in /proc/"$1"/fd/*; do
        [ "$fd" -ef "$LINUXLAB_TEST_LOCK_FILE" ] && return 0
    done
    return 1
}
check "  and has no descriptor on the lock file" not holds_lock "$background"
restore "$backups/x.tar"
check "  another operation gets the lock as soon as the backup ends" succeeded
kill "$background" 2>/dev/null || true

echo "== the production project's lock is fixed"
reset
fixture "$backups/x.tar"
: >"$STUB_LOG"
run env LINUXLAB_PROJECT=linuxlab-prod LINUXLAB_TEST_LOCK_FILE="$work/other.lock" "$ops/backup.sh" "$release"
check "LINUXLAB_TEST_LOCK_FILE is refused with the production project" failed
check "  and says so" err_says "LINUXLAB_TEST_LOCK_FILE is for test projects only"
check "  before any call" test ! -s "$STUB_LOG"
for command in "restore.sh $backups/x.tar" "migrate.sh"; do
    : >"$STUB_LOG"
    # shellcheck disable=SC2086 # Split on purpose: the script and its arguments.
    set -- $command
    run env LINUXLAB_PROJECT=linuxlab-prod LINUXLAB_TEST_LOCK_FILE="$work/other.lock" \
        "$ops/$1" "$release" "${@:2}"
    check "  $1 refuses it too, before any call" test "$rc" != 0 -a ! -s "$STUB_LOG"
    check "  and says so" err_says "LINUXLAB_TEST_LOCK_FILE is for test projects only"
done
: >"$STUB_LOG"
run env -u LINUXLAB_TEST_LOCK_FILE LINUXLAB_PROJECT=linuxlab-prod LINUXLAB_TEST_BACKUP_RUN=$fixed \
    "$ops/backup.sh" "$release"
check "LINUXLAB_TEST_BACKUP_RUN is refused with the production project" failed
check "  before any call" test ! -s "$STUB_LOG"

# The production project uses /run/linuxlab-ops.lock. As root (a container), hold it
# from another process and check that a production backup is refused because of it;
# otherwise (CI's user) the backup cannot even open it, and says which file it tried.
reset
canonical=/run/linuxlab-ops.lock
if [ -w /run ]; then
    rm -f "$work"/hold.*
    flock -n "$canonical" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
    held=$!
    wait_for "$work/hold.started"
    run env -u LINUXLAB_TEST_LOCK_FILE LINUXLAB_PROJECT=linuxlab-prod "$ops/backup.sh" "$release"
    check "the production project takes $canonical: refused while another process holds it" failed
    check "  and names it" err_says "another backup, restore, migration or content sync is running (lock $canonical)"
    release_held
else
    run env -u LINUXLAB_TEST_LOCK_FILE LINUXLAB_PROJECT=linuxlab-prod "$ops/backup.sh" "$release"
    check "the production project uses $canonical (not writable here: refused, naming it)" \
        err_says "cannot open the lock file $canonical"
fi
check "  without dumping" not_called "pg_dump -U"

reset
rm -f "$work"/hold.*
flock -n "$LINUXLAB_TEST_LOCK_FILE" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
held=$!
wait_for "$work/hold.started"
run env LINUXLAB_LOCK_FILE="$work/other.lock" "$ops/backup.sh" "$release"
check "LINUXLAB_LOCK_FILE (no longer read) does not move the lock" failed
check "  without dumping" not_called "pg_dump -U"
release_held

echo "== migrate.sh"
reset
start=$(date +%s)
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" "$ops/migrate.sh" "$release"
check "migrate.sh runs its own backup and restore under its lock, without deadlock" succeeded
check "  it backed up into a package" test "$(files 'linuxlab-*-pre-migrate-*.tar' | wc -l)" = 1
check "  it restored that package" called "CREATE DATABASE restore_"
check "  it migrated" called "alembic upgrade head"
check "  in well under the timeout" test $(($(date +%s) - start)) -lt 60
backup
check "  and the lock is free afterwards: backup.sh runs" succeeded

[ "$failures" -eq 0 ] || { echo "$failures checks failed" >&2; exit 1; }
echo "all backup cases passed"
