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
# Nothing here prints a secret.

set -euo pipefail
# Git Bash on Windows rewrites arguments that look like paths: container paths
# must reach docker as written, and host paths go through host_path.
docker() { MSYS_NO_PATHCONV=1 command docker "$@"; }

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "$OPS_DIR/.." && pwd)
ENV_FILE=${LINUXLAB_ENV_FILE:-/etc/linuxlab/production.env}
PROJECT=${LINUXLAB_PROJECT:-linuxlab-prod}

fail() {
    echo "error: $*" >&2
    exit 1
}

note() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >&2
}

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

# One operation at a time: backup.sh, restore.sh and migrate.sh take the same lock,
# without waiting, and stop when another one holds it. The lock is held on file
# descriptor 9, which the scripts migrate.sh starts (backup.sh, restore.sh) inherit:
# flock on that same open file succeeds at once, so they run under their parent's
# lock instead of waiting for it. Run on their own, they open the file and take it.
LOCK_FILE=${LINUXLAB_LOCK_FILE:-/run/linuxlab-ops.lock}
take_lock() {
    command -v flock >/dev/null || fail "flock (util-linux) is required"
    if ! { [ -e /dev/fd/9 ] && [ /dev/fd/9 -ef "$LOCK_FILE" ]; }; then
        exec 9>>"$LOCK_FILE" || fail "cannot open the lock file $LOCK_FILE"
    fi
    flock -n 9 || fail "another backup, restore or migration is running (lock $LOCK_FILE); nothing was done"
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

# Renames PARTIAL to FINAL, its checksum first: a file under its final name always
# has a checksum, and one without it is refused by restore.sh.
finish() {
    mv -- "$2.sha256.partial" "$2.sha256"
    mv -- "$1" "$2"
}
