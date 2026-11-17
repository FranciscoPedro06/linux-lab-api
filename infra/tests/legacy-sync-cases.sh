#!/usr/bin/env bash
# A rollback of the application without a downgrade (docs/operations.md#migrations):
# the content sync of a release of before questions, run on this release's schema,
# then this release's again. The older release inserts its content_syncs row without
# the question columns; the defaults must record "no questions" for it, and this
# release must go on recording the real values.
#
#   infra/tests/legacy-sync-cases.sh LEGACY_CHECKOUT
#
# LEGACY_CHECKOUT is a checkout of the older release (CI: a worktree at 5f733b6, main
# before questions). Its code runs from its own src/, ahead of this repository's
# installed package on the Python path, in this repository's environment (uv), and
# syncs its own synthetic content; this release's code syncs tests/fixtures/content.
# A database made for this run next to the one of DATABASE_URL (a role that can
# create databases), migrated to this release's head, dropped at the end.
set -euo pipefail

if [ $# -ne 1 ] || [ ! -d "$1/src/linuxlab" ]; then
    echo "usage: legacy-sync-cases.sh LEGACY_CHECKOUT" >&2
    exit 2
fi
legacy=$(cd "$1" && pwd)
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
: "${DATABASE_URL:?DATABASE_URL is required}"
cd "$repo"

name="linuxlab_legacy_$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
admin_url=$DATABASE_URL
url="${DATABASE_URL%/*}/$name"
EMPTY_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

# admin STATEMENT: run on the database of DATABASE_URL, outside a transaction.
admin() {
    uv run --no-sync python - "$admin_url" "$1" <<'PY'
import asyncio
import sys

import asyncpg


async def main(url: str, statement: str) -> None:
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


asyncio.run(main(*sys.argv[1:]))
PY
}
work=$(mktemp -d)
cleanup() {
    admin "DROP DATABASE IF EXISTS $name WITH (FORCE)" || true
    rm -rf "$work"
}
trap cleanup EXIT

failures=0
check() {
    local what=$1
    shift
    if "$@"; then echo "ok    $what"; else echo "FAIL  $what" >&2; failures=$((failures + 1)); fi
}

# The sync of the older release: its cli, its code, its content.
legacy_python() { DATABASE_URL=$url PYTHONPATH="$legacy/src" uv run --no-sync python "$@"; }
legacy_sync() {
    legacy_python -c 'import sys; from linuxlab.cli import main; sys.exit(main())' \
        content sync --content-dir "$legacy/tests/fixtures/content" --source-commit "$1"
}
current_sync() {
    DATABASE_URL=$url uv run --no-sync linuxlab content sync \
        --content-dir "$repo/tests/fixtures/content" --source-commit "$1"
}
# state: what the last content_syncs row records, and what this release's fingerprint
# queries give now ("recorded", "missions", "questions" and "question_versions" lines).
state() {
    DATABASE_URL=$url uv run --no-sync python - <<'PY'
import asyncio

from sqlalchemy import text

from linuxlab.config import get_settings
from linuxlab.content.sync import FINGERPRINT, QUESTION_FINGERPRINT
from linuxlab.db import create_engine


async def main() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            last = (
                await connection.execute(
                    text(
                        "SELECT coalesce(source_commit, '-'), missions, fingerprint,"
                        " questions, question_fingerprint"
                        " FROM content_syncs ORDER BY id DESC LIMIT 1"
                    )
                )
            ).one()
            missions = (await connection.execute(FINGERPRINT)).one()
            questions = (await connection.execute(QUESTION_FINGERPRINT)).one()
            versions = (
                await connection.execute(text("SELECT count(*) FROM question_versions"))
            ).scalar_one()
    finally:
        await engine.dispose()
    print("recorded", *last)
    print("missions", *missions)
    print("questions", *questions)
    print("question_versions", versions)


asyncio.run(main())
PY
}
field() { sed -n "s/^$1 //p" <<<"$2"; }
indent() { sed 's/^/    /'; }
recorded_missions() { field recorded "$1" | cut -d' ' -f2,3; }
recorded_questions() { field recorded "$1" | cut -d' ' -f4,5; }
recorded_commit() { field recorded "$1" | cut -d' ' -f1; }

# commit X: a full commit SHA of 40 times X.
commit() { head -c 40 /dev/zero | tr '\0' "$1"; }

admin "CREATE DATABASE $name"
DATABASE_URL=$url uv run --no-sync alembic upgrade head >/dev/null
echo "database $name at this release's head: $(DATABASE_URL=$url uv run --no-sync alembic current 2>/dev/null | tail -n 1)"

# The older release is what runs, not this one.
where=$(legacy_python -c 'import linuxlab.content.models as m; print(m.__file__, hasattr(m, "Question"), hasattr(m.ContentSync, "questions"))')
echo "the older release's code: $where"
check "the older release's code is the one imported, without questions" \
    test "$where" = "$legacy/src/linuxlab/content/models.py False False"

echo "== the older release syncs on this schema, no question in the database"
if legacy_sync "$(commit a)" >"$work/legacy-sync-1.log" 2>&1; then
    check "its sync succeeds" true
else
    check "its sync succeeds" false
    cat "$work/legacy-sync-1.log" >&2
fi
sed 's/^/    /' "$work/legacy-sync-1.log"
now=$(state)
indent <<<"$now"
check "its row records its commit" test "$(recorded_commit "$now")" = "$(commit a)"
check "  its missions, as this release's fingerprint query computes them" \
    test "$(recorded_missions "$now")" = "$(field missions "$now")"
check "  some missions were synced" test "$(field missions "$now" | cut -d' ' -f1)" -gt 0
check "  and, by the defaults, no questions: 0 and the SHA-256 of nothing" \
    test "$(recorded_questions "$now")" = "0 $EMPTY_SHA256"
check "  which is the fingerprint of the database's questions (none)" \
    test "$(field questions "$now")" = "0 $EMPTY_SHA256"

echo "== this release syncs, with questions"
current_sync "$(commit b)" | sed 's/^/    /'
now=$(state)
indent <<<"$now"
with_questions=$(field questions "$now")
check "its row records its commit" test "$(recorded_commit "$now")" = "$(commit b)"
check "  the real questions: 3, and their fingerprint" \
    test "$(recorded_questions "$now")" = "$with_questions" -a "${with_questions%% *}" = 3
check "  not the defaults" test "$(recorded_questions "$now" | cut -d' ' -f2)" != "$EMPTY_SHA256"
check "  and its missions" test "$(recorded_missions "$now")" = "$(field missions "$now")"

echo "== the application rolled back: the older release syncs again, questions in the database"
if legacy_sync "$(commit c)" >"$work/legacy-sync-2.log" 2>&1; then
    check "its sync succeeds" true
else
    check "its sync succeeds" false
    cat "$work/legacy-sync-2.log" >&2
fi
sed 's/^/    /' "$work/legacy-sync-2.log"
now=$(state)
indent <<<"$now"
check "its row records its commit and its missions" \
    test "$(recorded_commit "$now") $(recorded_missions "$now")" = "$(commit c) $(field missions "$now")"
check "  and the defaults for the questions it does not know" \
    test "$(recorded_questions "$now")" = "0 $EMPTY_SHA256"
check "the questions are untouched: same fingerprint, same 3 versions" \
    test "$(field questions "$now") $(field question_versions "$now")" = "$with_questions 3"
# backup.sh compares the last row with the database: they differ here, so a backup
# records no content commit ("changed"), which is right: commit c does not describe
# the questions in the database.
check "  so the row does not describe them, and a backup records no content commit" \
    test "$(recorded_questions "$now")" != "$(field questions "$now")"

echo "== this release syncs again"
current_sync "$(commit d)" | sed 's/^/    /'
now=$(state)
indent <<<"$now"
check "its row records the real questions again, no new version" \
    test "$(recorded_questions "$now") $(field question_versions "$now")" = "$with_questions 3"
check "  and its missions" test "$(recorded_missions "$now")" = "$(field missions "$now")"

if [ "$failures" -gt 0 ]; then
    echo "$failures checks failed" >&2
    exit 1
fi
echo "all checks passed"
