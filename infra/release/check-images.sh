#!/usr/bin/env bash
# Checks what the release images really contain, by running them, before any of
# them is used: no mission content or tests, no secrets in their configuration or
# build history, the API pinned to one non-root process, the lab image without
# setuid binaries. Each option names an image to check; CI checks --api and --lab.
#
#   infra/release/check-images.sh [--api IMAGE] [--lab IMAGE] [--edge IMAGE]
set -euo pipefail
# Paths below are inside the images; Git Bash on Windows would rewrite them.
export MSYS_NO_PATHCONV=1

api='' lab='' edge=''
while [ $# -gt 0 ]; do
    case "$1" in
        --api) api=$2; shift 2 ;;
        --lab) lab=$2; shift 2 ;;
        --edge) edge=$2; shift 2 ;;
        *) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
    esac
done

failures=0
check() {
    local name=$1
    shift
    if "$@"; then
        echo "ok    $name"
    else
        echo "FAIL  $name" >&2
        failures=$((failures + 1))
    fi
}

# Runs a shell script in the image, without network, as the given user.
inside() {
    local image=$1 user=$2 script=$3
    docker run --rm --network none --user "$user" --entrypoint sh "$image" -c "$script"
}

# Files that only mission content has: the private repository's layout
# (docs/missions.md), whatever directory it was copied to. A failing find fails the
# check, so an empty result always means it searched.
CONTENT_FILES='find / -xdev \( -name mission.yaml -o -name briefing.md -o -name explanation.md -o -name "solution-*.sh" -o -name "counter-*.sh" -o -name linux-lab-content \) -print'

no_mission_content() {
    local found
    found=$(inside "$1" 0 "$CONTENT_FILES") || return 1
    [ -z "$found" ] || { echo "$found" >&2; return 1; }
}

no_setuid_files() {
    local found
    found=$(inside "$1" 0 'find / -xdev -type f -perm /6000 -print') || return 1
    [ -z "$found" ] || { echo "$found" >&2; return 1; }
}

no_secrets_in_config() {
    local image=$1 env history
    env=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$image")
    history=$(docker history --no-trunc --format '{{.CreatedBy}}' "$image")
    ! grep -i -E 'password|secret|token|invite|database_url|authtoken' <<<"$env$history"
}

if [ -n "$api" ]; then
    check "api: content/ holds only .gitkeep" \
        test "$(inside "$api" 0 'find /app/content -type f')" = /app/content/.gitkeep
    check "api: no tests" inside "$api" 0 'test ! -e /app/tests'
    check "api: no mission content anywhere" no_mission_content "$api"
    check "api: no secrets in environment or history" no_secrets_in_config "$api"
    check "api: runs as uid 10001" \
        test "$(inside "$api" linuxlab 'id -u')" = 10001
    check "api: default user is not root" \
        test "$(docker image inspect --format '{{.Config.User}}' "$api")" = linuxlab
    check "api: command pins one worker" \
        grep -q -- '--workers 1 ' <<<"$(docker image inspect --format '{{join .Config.Cmd " "}}' "$api") "
fi

if [ -n "$lab" ]; then
    check "lab: no setuid or setgid files" no_setuid_files "$lab"
    check "lab: default user is the student" \
        test "$(docker image inspect --format '{{.Config.User}}' "$lab")" = student
    check "lab: no mission content anywhere" no_mission_content "$lab"
    check "lab: no secrets in environment or history" no_secrets_in_config "$lab"
fi

if [ -n "$edge" ]; then
    check "edge: frontend entry page present" inside "$edge" 0 'test -f /srv/web/index.html'
    check "edge: no mission content anywhere" no_mission_content "$edge"
    check "edge: no secrets in environment or history" no_secrets_in_config "$edge"
    check "edge: Caddyfile valid" docker run --rm --network none -e SITE_ADDRESS=lab.example.org \
        "$edge" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
fi

if [ "$failures" -gt 0 ]; then
    echo "$failures image checks failed" >&2
    exit 1
fi
