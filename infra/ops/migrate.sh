#!/usr/bin/env bash
# Upgrades the deployment's database to the release's migrations, with the release's
# API image, after a backup that is checked by restoring it into a new database.
# Run it before starting the release's API. See docs/operations.md#migrations.
#
#   infra/ops/migrate.sh RELEASE_FILE
#
# Exit status 0 only when the database is at the release's head: already, or after
# the upgrade. Any query that cannot say where the database or the release stands
# stops the script with its own error output.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

# One revision id per line, as `alembic heads` and `alembic current` print them
# ("<id>" or "<id> (head)"); anything else is not an answer this script can trust.
revisions() {
    local output=$1 what=$2 line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        [[ $line =~ ^[0-9a-f]+(\ \(head\))?$ ]] || fail "unexpected output from alembic $what: $line"
        echo "${line%% *}"
    done <<<"$output"
}

alembic() {
    compose run --rm --no-deps -T task alembic "$@"
}

load_release "${1:-}"
compose up -d --wait postgres >/dev/null

# The migration history is linear: a release has exactly one head (CI runs
# `alembic check` and every migration names a single down revision).
output=$(alembic heads) || fail "could not read the release's migration head"
heads=$(revisions "$output" heads)
[ -n "$heads" ] || fail "alembic heads printed no revision"
[ "$(wc -l <<<"$heads")" = 1 ] || fail "the release has several migration heads: $(paste -sd ' ' <<<"$heads")"
head=$heads

# No revision is a database that was never migrated, which an upgrade creates.
output=$(alembic current) || fail "could not read the database's revision"
current=$(revisions "$output" current)
[ "$(wc -l <<<"$current")" -le 1 ] || fail "the database is at several revisions: $(paste -sd ' ' <<<"$current")"
note "database at: ${current:-no revision}; release at: $head"
if [ "$current" = "$head" ]; then
    note "already at the release's head; nothing to migrate"
    exit 0
fi

dump=$("$OPS_DIR/backup.sh" "$RELEASE" "pre-migrate-${API_SHA:0:12}") \
    || note "copying the backup off the host failed; the local backup is checked below"
[ -f "$dump" ] || fail "no backup was written; not migrating"
"$OPS_DIR/restore.sh" "$RELEASE" "$dump" || fail "the backup did not restore; not migrating"

note "migrating"
alembic upgrade head || fail "alembic upgrade failed; backup before: $dump"
output=$(alembic current) || fail "could not read the database's revision after the upgrade"
after=$(revisions "$output" current)
[ "$after" = "$head" ] || fail "after the upgrade the database is at ${after:-no revision}, not $head"
note "database now at: $after (backup before: $dump)"
