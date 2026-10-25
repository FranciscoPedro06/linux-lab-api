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
        [ -n "$image" ] && [ "$actual" = "$recorded" ] \
            || fail "$image is not the image recorded in $RELEASE (rebuild or use the right release)"
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
