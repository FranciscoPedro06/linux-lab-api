#!/usr/bin/env bash
# Cases of infra/ops/backup.sh, restore.sh and migrate.sh with the docker CLI replaced
# by infra/tests/stubs/docker: no container or database is involved, and everything
# is written under a temporary directory. Each case checks what the script did (the
# files it left, the calls it made), not only its exit status.
#
#   infra/tests/backup-cases.sh
#
# Needs flock (util-linux), gzip, tar and timeout.
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
export LINUXLAB_LOCK_FILE="$work/ops.lock"
backups="$work/backups"
release="$work/release.env"
printf '%s\n' API_SHA=0123456789abcdef0123456789abcdef01234567 \
    API_IMAGE=api API_IMAGE_ID=sha256:stub LAB_IMAGE=lab LAB_IMAGE_ID=sha256:stub \
    EDGE_IMAGE=edge EDGE_IMAGE_ID=sha256:stub >"$release"
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

failed() { [ "$rc" != 0 ]; }
succeeded() { [ "$rc" = 0 ]; }
called() { grep -q -- "$1" "$STUB_LOG"; }
not_called() { ! grep -q -- "$1" "$STUB_LOG"; }
count_calls() { grep -c -- "$1" "$STUB_LOG" || true; }
files() { find "$backups" -maxdepth 1 -type f -name "$1" | sort; }
none() { [ -z "$(files "$1")" ]; }
one() { [ "$(files "$1" | wc -l)" = 1 ]; }
no_partials() { none '*.partial'; }

# wait_for FILE: up to 30 s.
wait_for() {
    for _ in $(seq 300); do
        [ -e "$1" ] && return 0
        sleep 0.1
    done
    echo "timed out waiting for $1" >&2
    return 1
}

# A dump in the stub's format with its checksum, written as an operator would find it.
dump_data='rows of every table'
dump_sum=$(printf '%s' "$dump_data" | sha256sum | cut -c1-64)
fixture() {
    local file=$1
    mkdir -p "$(dirname "$file")"
    printf 'PGDMP-STUB\n%s\nEND %s\n' "$dump_data" "$dump_sum" >"$file"
    printf '%s  %s\n' "$(sha256sum <"$file" | cut -c1-64)" "$(basename "$file")" >"$file.sha256"
}

# Moves a backup's stamp back by a month, as if it were an old backup.
age() { touch -d '30 days ago' "$@"; }

backup() { run "$ops/backup.sh" "$release" "$@"; }
restore() { run "$ops/restore.sh" "$release" "$@"; }

echo "== backup.sh: the database dump"
reset
backup
check "an intact dump is accepted" succeeded
check "  stdout is the path of the new dump" test -f "$(cat "$work/out")" -a "$(wc -l <"$work/out")" = 1
check "  the dump has a checksum" one 'linuxlab-*.dump.sha256'
check "  the checksum names the file, not a path" \
    grep -Eq '^[0-9a-f]{64}  linuxlab-[0-9TZ]+-scheduled\.dump$' "$(files 'linuxlab-*.dump.sha256')"
check "  no partial file is left" no_partials

for mode in truncated corrupt fail; do
    reset
    STUB_DUMP=$mode backup
    check "a $mode dump is rejected" failed
    check "  no dump is presented as a backup" none 'linuxlab-*.dump'
    check "  no checksum is written for it" none 'linuxlab-*.sha256'
    check "  no partial file is left" no_partials
    check "  nothing is printed as a backup" test ! -s "$work/out"
done

echo "== backup.sh: Caddy's data"
reset
STUB_CADDY=valid backup
check "an intact Caddy archive is accepted" succeeded
check "  the archive and its checksum are kept" one 'caddy-data-*.tar.gz'
check "  its checksum names the file" \
    grep -Eq '^[0-9a-f]{64}  caddy-data-[0-9TZ]+-scheduled\.tar\.gz$' "$(files 'caddy-data-*.sha256')"
check "  the archive lists" tar -tzf "$(files 'caddy-data-*.tar.gz')"
for mode in truncated corrupt flipped fail; do
    reset
    STUB_CADDY=$mode backup
    check "a $mode Caddy archive is rejected" failed
    check "  no archive is presented as a backup" none 'caddy-data-*.tar.gz'
    check "  no partial file is left" no_partials
done

echo "== backup.sh: copies off the host and retention"
reset BACKUP_KEEP_DAYS=1 "BACKUP_COPY_COMMAND=false"
fixture "$backups/linuxlab-20260101T031500Z-scheduled.dump"
age "$backups"/linuxlab-20260101T031500Z-scheduled.*
STUB_CADDY=valid backup
check "a failed copy off the host is a failure" failed
check "  the failure is reported" grep -q "FAILED" "$work/err"
check "  the old backup is still there" one 'linuxlab-20260101T031500Z-scheduled.dump'
check "  the old checksum is still there" one 'linuxlab-20260101T031500Z-scheduled.dump.sha256'
check "  the new backup is kept too: two dumps in the directory" \
    test "$(files 'linuxlab-*.dump' | wc -l)" = 2

reset BACKUP_KEEP_DAYS=1 "BACKUP_COPY_COMMAND=cp -t $work/offsite"
mkdir -p "$work/offsite"
fixture "$backups/linuxlab-20260101T031500Z-scheduled.dump"
age "$backups"/linuxlab-20260101T031500Z-scheduled.*
STUB_CADDY=valid backup
check "a successful copy is a success" succeeded
check "  each new file was copied (dump, Caddy and their checksums)" \
    test "$(find "$work/offsite" -type f | wc -l)" = 4
check "  an old backup is not removed on the copy command's word alone" \
    one 'linuxlab-20260101T031500Z-scheduled.dump'
check "  the removal is reported as suspended" grep -qi "suspended" "$work/err"

reset
backup
check "without BACKUP_COPY_COMMAND the backup succeeds" succeeded
check "  and says it is on this host only" grep -q "BACKUP_COPY_COMMAND not set" "$work/err"

echo "== backup.sh: partial files and arguments"
reset
stale="$backups/linuxlab-20260101T031500Z-scheduled.dump.partial"
echo "an earlier run that never finished" >"$stale"
backup
check "a partial file of an earlier run is not a backup" test "$(cat "$work/out")" != "$stale"
check "  and is left as it is" test -f "$stale"
check "  and is reported" grep -q "partial" "$work/err"

reset
backup Not-A-Label
check "an invalid label is refused" failed
check "  before any dump" not_called pg_dump
reset
backup scheduled extra
check "an extra argument is refused" failed
check "  before any dump" not_called pg_dump

reset
STUB_HOLD="$work/hold" STUB_HOLD_ON=pg_dump timeout 120 "$ops/backup.sh" "$release" \
    >"$work/out" 2>"$work/err" &
pid=$!
wait_for "$work/hold.started"
kill -TERM "$pid"
touch "$work/hold.release"
rc=0
wait "$pid" || rc=$?
check "an interrupted backup fails" failed
check "  and leaves neither a dump nor a partial file" none 'linuxlab-*'

echo "== restore.sh: checksums"
reset
fixture "$backups/linuxlab-20260101T031500Z-scheduled.dump"
restore "$backups/linuxlab-20260101T031500Z-scheduled.dump"
check "an intact dump with its checksum restores" succeeded
check "  into a new database" called "CREATE DATABASE restore_"
check "  which is dropped afterwards" called "DROP DATABASE IF EXISTS restore_"

reset
fixture "$backups/x.dump"
rm "$backups/x.dump.sha256"
restore "$backups/x.dump"
check "a dump without a checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.dump"
echo "tampered" >>"$backups/x.dump"
restore "$backups/x.dump"
check "a dump changed after its checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.dump"
sed -i 's/[0-9a-f]\{64\}/'"$(printf '0%.0s' $(seq 64))"'/' "$backups/x.dump.sha256"
restore "$backups/x.dump"
check "a changed checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
fixture "$backups/x.dump"
sed -i 's/  x\.dump$/  other.dump/' "$backups/x.dump.sha256"
restore "$backups/x.dump"
check "a checksum naming another file is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
mkdir -p "$backups/s"
printf 'PGDMP-STUB\n%s\n' "$dump_data" >"$backups/s/t.dump"
printf '%s  %s\n' "$(sha256sum <"$backups/s/t.dump" | cut -c1-64)" t.dump >"$backups/s/t.dump.sha256"
restore "$backups/s/t.dump"
check "a truncated dump with a matching checksum is refused" failed
check "  before any database is created" not_called "CREATE DATABASE"

reset
backup
dump=$(cat "$work/out")
mkdir -p "$work/elsewhere"
mv "$dump" "$dump.sha256" "$work/elsewhere/"
: >"$STUB_LOG"
restore "$work/elsewhere/$(basename "$dump")"
check "a backup moved to another directory still checks and restores" succeeded
check "  into a new database" called "CREATE DATABASE restore_"

echo "== restore.sh: arguments"
reset
fixture "$backups/x.dump"
restore "$backups/x.dump" --keep
check "--keep is accepted" succeeded
check "  the restored database is kept" not_called "DROP DATABASE"

for args in "--kep" "--keep extra" "keep" "--KEEP"; do
    reset
    fixture "$backups/x.dump"
    # shellcheck disable=SC2086 # Split on purpose: the arguments of the case.
    restore "$backups/x.dump" $args
    check "restore.sh refuses '$args'" failed
    check "  without creating or dropping any database" not_called "DATABASE"
done
reset
restore
check "restore.sh without a dump is refused" failed

reset
fixture "$backups/x.dump"
STUB_RESTORE_RC=1 restore "$backups/x.dump"
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
fixture "$backups/x.dump"
hold pg_dump "$ops/backup.sh" "$release"
restore "$backups/x.dump"
check "restore.sh is refused while a backup runs" failed
check "  and says another operation is running" grep -qi "another" "$work/err"
check "  without creating any database" not_called "CREATE DATABASE"
release_held
check "  the backup itself completes" test "$held_rc" = 0

reset
fixture "$backups/x.dump"
hold "CREATE DATABASE" "$ops/restore.sh" "$release" "$backups/x.dump"
backup
check "backup.sh is refused while a restore runs" failed
check "  without dumping" not_called pg_dump
release_held
check "  the restore itself completes" test "$held_rc" = 0

reset
hold "alembic upgrade" env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" \
    "$ops/migrate.sh" "$release"
before=$(count_calls pg_dump)
backup
check "backup.sh is refused while a migration runs" failed
check "  without dumping" test "$(count_calls pg_dump)" = "$before"
fixture "$backups/x.dump"
restore "$backups/x.dump"
check "restore.sh is refused while a migration runs" failed
release_held
check "  the migration itself completes" test "$held_rc" = 0

reset
flock -n "$LINUXLAB_LOCK_FILE" -c "touch '$work/hold.started'; while [ ! -e '$work/hold.release' ]; do sleep 0.1; done" &
held=$!
wait_for "$work/hold.started"
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" "$ops/migrate.sh" "$release"
check "migrate.sh is refused while another process holds the lock" failed
check "  before reading or changing the database" not_called alembic
release_held

reset
start=$(date +%s)
run env STUB_HEADS="$HEAD" STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER="$HEAD" "$ops/migrate.sh" "$release"
check "migrate.sh runs its own backup and restore under its lock, without deadlock" succeeded
check "  it backed up" called pg_dump
check "  it restored that backup" called "CREATE DATABASE restore_"
check "  it migrated" called "alembic upgrade head"
check "  in well under the timeout" test $(($(date +%s) - start)) -lt 60
backup
check "  and the lock is free afterwards: backup.sh runs" succeeded

[ "$failures" -eq 0 ] || { echo "$failures checks failed" >&2; exit 1; }
echo "all backup cases passed"
