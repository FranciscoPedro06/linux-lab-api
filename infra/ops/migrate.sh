#!/usr/bin/env bash
# Upgrades the deployment's database to the release's migrations, with the release's
# API image, after a backup that is checked by restoring it into a new database.
# Run it before starting the release's API. See docs/operations.md#migrations.
#
#   infra/ops/migrate.sh RELEASE_FILE
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

load_release "${1:-}"
compose up -d --wait postgres >/dev/null

current=$(compose run --rm --no-deps task alembic current 2>/dev/null | tail -n 1)
heads=$(compose run --rm --no-deps task alembic heads 2>/dev/null | tail -n 1)
note "database at: ${current:-empty}; release at: $heads"
if [ "${current%% *}" = "${heads%% *}" ]; then
    note "already at the release's head; nothing to migrate"
    exit 0
fi

dump=$("$OPS_DIR/backup.sh" "$RELEASE" "pre-migrate-${API_SHA:0:12}") \
    || note "copying the backup off the host failed; the local backup is checked below"
[ -f "$dump" ] || fail "no backup was written; not migrating"
"$OPS_DIR/restore.sh" "$RELEASE" "$dump" || fail "the backup did not restore; not migrating"

note "migrating"
compose run --rm --no-deps task alembic upgrade head
after=$(compose run --rm --no-deps task alembic current 2>/dev/null | tail -n 1)
note "database now at: $after (backup before: $dump)"
