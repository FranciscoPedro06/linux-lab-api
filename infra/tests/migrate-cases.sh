#!/usr/bin/env bash
# Decision cases of infra/ops/migrate.sh, with the docker CLI replaced by
# infra/tests/stubs/docker: no container or database is involved. Checks the exit
# status, whether `alembic upgrade head` was run, and that a failed query neither
# reports success nor hides its error output.
#
#   infra/tests/migrate-cases.sh
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
migrate="$here/../ops/migrate.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export PATH="$here/stubs:$PATH" STUB_LOG="$work/calls"
export LINUXLAB_ENV_FILE="$work/env" LINUXLAB_PROJECT=linuxlab-migrate-cases
printf '%s\n' "BACKUP_DIR=$work/backups" >"$LINUXLAB_ENV_FILE"
release="$work/release.env"
printf '%s\n' API_SHA=0123456789abcdef0123456789abcdef01234567 \
    API_IMAGE=api API_IMAGE_ID=sha256:stub LAB_IMAGE=lab LAB_IMAGE_ID=sha256:stub \
    EDGE_IMAGE=edge EDGE_IMAGE_ID=sha256:stub >"$release"

HEAD='e6b40c7d9a18 (head)\n'
failures=0

# case NAME EXPECTED_STATUS EXPECTED_UPGRADE [VAR=VALUE...]
#   EXPECTED_STATUS: 0 or "fail"; EXPECTED_UPGRADE: upgrade or no-upgrade
case_() {
    local name=$1 status=$2 upgrade=$3
    shift 3
    : >"$STUB_LOG"
    local rc=0
    env STUB_HEADS="$HEAD" STUB_CURRENT="" STUB_AFTER="$HEAD" "$@" \
        "$migrate" "$release" >"$work/out" 2>&1 || rc=$?
    local ran=no-upgrade
    grep -q "alembic upgrade head" "$STUB_LOG" && ran=upgrade
    local problems=()
    if [ "$status" = 0 ] && [ "$rc" != 0 ]; then problems+=("exit $rc, expected 0"); fi
    if [ "$status" = fail ] && [ "$rc" = 0 ]; then problems+=("exit 0, expected a failure"); fi
    [ "$ran" = "$upgrade" ] || problems+=("$ran, expected $upgrade")
    if [ "$status" = fail ] && grep -q "nothing to migrate" "$work/out"; then
        problems+=("reported nothing to migrate")
    fi
    if [ "${#problems[@]}" = 0 ]; then
        echo "ok    $name"
    else
        echo "FAIL  $name: ${problems[*]}" >&2
        sed 's/^/      /' "$work/out" >&2
        failures=$((failures + 1))
    fi
}

shows() {
    grep -q -- "$1" "$work/out" || { echo "FAIL  output lacks: $1" >&2; failures=$((failures + 1)); }
}

case_ "alembic heads fails" fail no-upgrade STUB_HEADS_RC=1
shows "alembic heads diagnostics"
case_ "alembic heads prints nothing" fail no-upgrade STUB_HEADS=""
case_ "alembic current fails" fail no-upgrade STUB_CURRENT_RC=1
shows "alembic current diagnostics"
case_ "new database, no revision yet: migrated" 0 upgrade STUB_CURRENT=""
case_ "database at the release's head: nothing to migrate" 0 no-upgrade STUB_CURRENT="$HEAD"
case_ "database behind: migrated" 0 upgrade STUB_CURRENT='c4e1a7b39d52\n'
case_ "upgrade fails" fail upgrade STUB_CURRENT='c4e1a7b39d52\n' STUB_UPGRADE_RC=1
shows "alembic upgrade diagnostics"
case_ "upgrade leaves the database short of the head" fail upgrade \
    STUB_CURRENT='c4e1a7b39d52\n' STUB_AFTER='a3f81c5d2e94\n'
# The migration history is linear (one head, checked by alembic check in CI): a
# release with several heads, or a database at several revisions, is refused.
case_ "release with two heads" fail no-upgrade STUB_HEADS='aaaaaaaaaaaa (head)\nbbbbbbbbbbbb (head)\n'
case_ "database at two revisions" fail no-upgrade STUB_CURRENT='aaaaaaaaaaaa\nbbbbbbbbbbbb\n'
case_ "unexpected heads output" fail no-upgrade STUB_HEADS='Traceback (most recent call last)\n'

[ "$failures" -eq 0 ]
