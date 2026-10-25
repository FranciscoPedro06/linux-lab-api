#!/usr/bin/env bash
# Checks the production environment file before a deployment, without printing any
# value: every required setting present and well formed, no example value left, the
# Docker socket group matching this host, the file readable only by its owner.
#
#   infra/release/check-env.sh /etc/linuxlab/production.env [NGROK_ENV_FILE]
#
# With NGROK_ENV_FILE, checks the entry through ngrok (infra/compose.ngrok.yml)
# instead: NGROK_URL is the public origin, and the agent's authtoken is set in that
# file, readable only by its owner.
set -euo pipefail

file=${1:?usage: check-env.sh ENV_FILE [NGROK_ENV_FILE]}
ngrok_file=${2:-}
[ -f "$file" ] || { echo "error: $file not found" >&2; exit 1; }

failures=0
problem() {
    echo "FAIL  $*" >&2
    failures=$((failures + 1))
}

# The last assignment of a variable, as docker compose reads it (no shell expansion).
value() {
    sed -n "s/^$1=//p" "$file" | tail -n 1
}

for name in SITE_ADDRESS PUBLIC_ORIGIN POSTGRES_PASSWORD DOCKER_SOCKET_GID LAB_DEPLOYMENT LAB_CAPACITY; do
    [ -n "$(value "$name")" ] || problem "$name is not set"
done

site=$(value SITE_ADDRESS)
[[ $site != *example.org* ]] || problem "SITE_ADDRESS is still the example"
[ "$(value PUBLIC_ORIGIN)" = "https://$site" ] || problem "PUBLIC_ORIGIN is not https://SITE_ADDRESS"
if [ -n "$ngrok_file" ]; then
    [ "$(value NGROK_URL)" = "https://$site" ] || problem "NGROK_URL is not https://SITE_ADDRESS"
    if [ -f "$ngrok_file" ]; then
        token=$(sed -n 's/^NGROK_AUTHTOKEN=//p' "$ngrok_file" | tail -n 1)
        [ -n "$token" ] || problem "NGROK_AUTHTOKEN is not set in $ngrok_file"
        ngrok_mode=$(stat -c %a "$ngrok_file" 2>/dev/null || echo unknown)
        [ "$ngrok_mode" = 600 ] || [ "$ngrok_mode" = 400 ] || problem "$ngrok_file has mode $ngrok_mode, expected 600"
    else
        problem "$ngrok_file not found"
    fi
fi
[[ $(value POSTGRES_PASSWORD) =~ ^[0-9a-f]{32,}$ ]] || problem "POSTGRES_PASSWORD is not at least 32 hex digits"
invite=$(value SIGNUP_INVITE_CODE)
[ -z "$invite" ] || [ ${#invite} -ge 20 ] || problem "SIGNUP_INVITE_CODE is shorter than 20 characters"
[[ $(value LAB_DEPLOYMENT) =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || problem "LAB_DEPLOYMENT is not a valid label value"
[[ $(value LAB_CAPACITY) =~ ^[1-9][0-9]*$ ]] || problem "LAB_CAPACITY is not a positive integer"

gid=$(value DOCKER_SOCKET_GID)
if [ -S /var/run/docker.sock ]; then
    [ "$gid" = "$(stat -c %g /var/run/docker.sock)" ] || problem "DOCKER_SOCKET_GID is not the group of /var/run/docker.sock"
else
    [[ $gid =~ ^[0-9]+$ ]] || problem "DOCKER_SOCKET_GID is not a number"
    echo "note  /var/run/docker.sock not found here; group not compared" >&2
fi

mode=$(stat -c %a "$file" 2>/dev/null || echo unknown)
[ "$mode" = 600 ] || [ "$mode" = 400 ] || problem "$file has mode $mode, expected 600"

if [ "$failures" -gt 0 ]; then
    echo "$failures problems in $file" >&2
    exit 1
fi
echo "ok    $file"
