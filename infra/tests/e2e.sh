#!/usr/bin/env bash
# End-to-end checks of the production stack (docs/deployment.md#checking-a-release):
# starts infra/compose.prod.yml with the release's images as a disposable project,
# with the synthetic missions of tests/fixtures/content, and checks it from clients
# on its public network, each with its own address, through Caddy only.
#
#   infra/tests/e2e.sh RELEASE_FILE [--runtime runsc|runc] [--keep]
#
# --runtime runc is for hosts without gVisor (Docker Desktop); it also runs the API
# with ENVIRONMENT=development, which is what allows runc. Everything is removed at
# the end unless --keep. Never run it with the production project name.
set -euo pipefail
# Container paths below must reach docker as written (Git Bash would rewrite them).
export MSYS_NO_PATHCONV=1

release='' runtime=runsc keep=false
while [ $# -gt 0 ]; do
    case "$1" in
        --runtime) runtime=$2; shift 2 ;;
        --keep) keep=true; shift ;;
        -*) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
        *) release=$1; shift ;;
    esac
done
[ -f "$release" ] || { echo "usage: e2e.sh RELEASE_FILE [--runtime runsc|runc] [--keep]" >&2; exit 2; }

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
project=linuxlab-e2e
deployment=e2e

value() { sed -n "s/^$1=//p" "$release" | tail -n 1; }
host_path() { if command -v cygpath >/dev/null; then cygpath -w "$1"; else echo "$1"; fi; }

api_image=$(value API_IMAGE)
for name in API LAB EDGE; do
    image=$(value "${name}_IMAGE")
    [ "$(docker image inspect --format '{{.Id}}' "$image")" = "$(value "${name}_IMAGE_ID")" ] \
        || { echo "error: $image is not the image recorded in $release" >&2; exit 1; }
done

work=$(mktemp -d)
chmod 700 "$work"
random() { docker run --rm --network none --entrypoint python "$api_image" -c "import secrets; print(secrets.token_hex($1))"; }
invite=$(random 16)
env_file="$work/e2e.env"
(
    umask 077
    cat >"$env_file" <<EOF
SITE_ADDRESS=linuxlab.localhost
PUBLIC_ORIGIN=https://linuxlab.localhost:8443
POSTGRES_PASSWORD=$(random 32)
SIGNUP_INVITE_CODE=$invite
DOCKER_SOCKET_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock --entrypoint stat "$api_image" -c %g /var/run/docker.sock)
LAB_DEPLOYMENT=$deployment
LAB_CAPACITY=4
PUBLIC_SUBNET=172.31.0.0/24
EDGE_SUBNET=172.31.1.0/24
DB_SUBNET=172.31.2.0/24
CADDY_EDGE_IP=172.31.1.2
API_EDGE_IP=172.31.1.3
E2E_RUNTIME=$runtime
E2E_ENVIRONMENT=$([ "$runtime" = runsc ] && echo production || echo development)
EOF
)

compose_files=(-f "$(host_path "$repo/infra/compose.prod.yml")"
    --env-file "$(host_path "$release")" --env-file "$(host_path "$env_file")")
compose() {
    docker compose -p "$project" "${compose_files[@]}" -f "$(host_path "$here/compose.e2e.yml")" "$@"
}

cleanup() {
    if [ "$keep" = false ]; then
        compose --profile tasks down --volumes --remove-orphans >/dev/null 2>&1 || true
        # Labs of this disposable deployment only.
        docker ps -aq --filter label=linuxlab.deployment=$deployment | xargs -r docker rm -f >/dev/null
        rm -rf "$work"
    else
        echo "kept project $project; env file $env_file"
    fi
}
trap cleanup EXIT

failures=0
result() {
    if [ "$1" = 0 ]; then echo "ok    $2"; else echo "FAIL  $2" >&2; failures=$((failures + 1)); fi
}
expect_ok() { local what=$1; shift; if "$@"; then result 0 "$what"; else result 1 "$what"; fi; }
expect_fails() { local what=$1; shift; if "$@" >/dev/null 2>&1; then result 1 "$what"; else result 0 "$what"; fi; }

echo "== configuration"
config=$(docker compose -p linuxlab-prod "${compose_files[@]}" config --format json)
check_config() {
    docker run -i --rm --network none --entrypoint python "$api_image" -c '
import json, sys
config = json.load(sys.stdin)
services, networks = config["services"], config["networks"]
problems = []
for name, service in services.items():
    if service.get("ports") and name != "caddy":
        problems.append(f"{name} publishes ports")
    if "WEB_CONCURRENCY" in (service.get("environment") or {}):
        problems.append(f"{name} sets WEB_CONCURRENCY")
ports = sorted(port["target"] for port in services["caddy"]["ports"])
if ports != [80, 443]:
    problems.append(f"caddy publishes {ports}")
api = services["api"]
command = api["command"]
caddy_ip = services["caddy"]["networks"]["edge"]["ipv4_address"]
if command[command.index("--workers") + 1] != "1":
    problems.append("api workers")
if "--proxy-headers" not in command or command[command.index("--forwarded-allow-ips") + 1] != caddy_ip:
    problems.append("api trusts something other than caddy")
if not api.get("container_name") or api.get("deploy", {}).get("replicas", 1) != 1:
    problems.append("api can be scaled")
if sorted(api["networks"]) != ["db", "edge"] or sorted(services["postgres"]["networks"]) != ["db"]:
    problems.append("api or postgres on an unexpected network")
if sorted(services["caddy"]["networks"]) != ["edge", "public"]:
    problems.append("caddy networks")
for name in ("edge", "db"):
    if not networks[name].get("internal"):
        problems.append(f"network {name} is not internal")
mounts = {name: [v["source"] for v in s.get("volumes", []) if v["type"] == "bind"] for name, s in services.items()}
if any("/var/run/docker.sock" in sources for name, sources in mounts.items() if name != "api"):
    problems.append("docker socket mounted outside the api")
print("\n".join(problems) or "ok")
sys.exit(1 if problems else 0)
' <<<"$config"
}
expect_ok "production compose: only Caddy publishes ports, one API process trusting only Caddy, internal networks, socket only in the API" \
    check_config

echo "== start"
compose up -d --wait postgres >/dev/null
compose run --rm task alembic upgrade head >/dev/null
compose run --rm -v "$(host_path "$repo/tests/fixtures/content"):/content:ro" \
    task linuxlab content sync --content-dir /content
compose up -d --wait >/dev/null
echo "stack up ($runtime)"

for _ in $(seq 30); do
    compose cp caddy:/data/caddy/pki/authorities/local/root.crt "$(host_path "$work/root.crt")" >/dev/null 2>&1 && break
    sleep 1
done
cp "$here/e2e.py" "$work/e2e.py"
network="${project}_public"
caddy_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"$network\").IPAddress}}" "$(compose ps -q caddy)")
postgres_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"${project}_db\").IPAddress}}" "$(compose ps -q postgres)")

client() {
    local address=$1
    shift
    docker run --rm --network "$network" --ip "$address" --add-host "linuxlab.localhost:$caddy_ip" \
        -e E2E_HOST=linuxlab.localhost -e E2E_ORIGIN=https://linuxlab.localhost:8443 -e E2E_INVITE="$invite" \
        -v "$(host_path "$work"):/e2e:ro" --entrypoint python "$api_image" /e2e/e2e.py "$@"
}

echo "== site through Caddy"
expect_ok "site checks" client 172.31.0.101 site

echo "== client addresses and the rate limit"
expect_ok "client A: 5 failed logins are counted" client 172.31.0.111 logins 5 401
expect_ok "client A: the 6th is limited" client 172.31.0.111 logins 1 429
expect_ok "client B: not limited by A's attempts" client 172.31.0.112 logins 1 401
expect_ok "client A: a forged X-Forwarded-For does not escape the limit" client 172.31.0.111 logins 1 429 198.51.100.7
expect_ok "client A: forging B's address does not escape either" client 172.31.0.111 logins 1 429 172.31.0.112
expect_ok "client A: forging Caddy's address does not escape either" client 172.31.0.111 logins 1 429 172.31.1.2
expect_ok "client C: forges D's address on 5 attempts" client 172.31.0.113 logins 5 401 172.31.0.114
expect_ok "client D: was not charged for C's attempts" client 172.31.0.114 logins 1 401
logins_seen=$(compose logs caddy 2>/dev/null | grep '"uri":"/api/auth/login"' | grep -o '"client_ip":"[0-9.]*"' | sort -u | tr '\n' ' ' || true)
seen_real_addresses() {
    grep -q '172.31.0.111' <<<"$logins_seen" && grep -q '172.31.0.113' <<<"$logins_seen" \
        && ! grep -q '198.51.100.7' <<<"$logins_seen"
}
expect_ok "Caddy logged the real client addresses, never the forged ones ($logins_seen)" \
    seen_real_addresses

echo "== what is reachable"
expect_ok "public network reaches Caddy on 443 (control)" client 172.31.0.120 connect "$caddy_ip" 443
expect_fails "public network cannot resolve the API" client 172.31.0.120 connect api 8000
expect_fails "public network cannot reach the API's address" client 172.31.0.120 connect 172.31.1.3 8000
expect_fails "public network cannot reach PostgreSQL" client 172.31.0.120 connect "$postgres_ip" 5432
expect_fails "public network cannot reach Caddy's admin endpoint" client 172.31.0.120 connect "$caddy_ip" 2019
in_api() { compose exec -T api python -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3)" "$@"; }
expect_ok "API reaches PostgreSQL (control)" in_api postgres 5432
expect_fails "API has no route to the internet" in_api 1.1.1.1 443
expect_fails "API cannot resolve internet names" in_api example.org 443

echo "== one API process"
# Every process in the container but this check: uvicorn alone, as pid 1.
processes=$(compose exec -T api python -c '
import os
others = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    if int(pid) == os.getpid():
        continue
    try:
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        continue
    others.append(pid + ":" + " ".join(arg.decode().rsplit("/", 1)[-1] for arg in argv[:2]))
print(",".join(others))')
expect_ok "one process in the API container, uvicorn ($processes)" \
    test "$processes" = "1:python uvicorn"
expect_fails "WEB_CONCURRENCY not set in the API" compose exec -T api printenv WEB_CONCURRENCY
expect_fails "a second API replica cannot start" compose up -d --no-recreate --scale api=2 api

echo "== a student's session"
expect_ok "flow: lab, terminal, validation, history, reset, switch, end, logout, other user" \
    client 172.31.0.130 flow ana@e2e.example bia@e2e.example

echo "== lab isolation and API restart"
client 172.31.0.140 restart caio@e2e.example >"$work/restart.log" 2>&1 &
restart_client=$!
for _ in $(seq 60); do grep -q holding "$work/restart.log" && break; sleep 1; done
lab=$(docker ps -q --filter label=linuxlab.deployment=$deployment | head -n 1)
if [ -n "$lab" ]; then
    expect_ok "lab container: runtime $runtime, no network, no mounts, read-only, not privileged" \
        test "$(docker inspect --format '{{.HostConfig.Runtime}} {{.HostConfig.NetworkMode}} {{len .Mounts}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}}' "$lab")" = "$runtime none 0 true false"
    expect_ok "lab: no Docker socket inside" docker exec "$lab" sh -c '! find / -xdev -name docker.sock 2>/dev/null | grep -q . && test ! -e /var/run/docker.sock'
    expect_ok "lab: only the loopback interface" test "$(docker exec "$lab" ip -o link | awk -F': ' '{print $2}')" = lo
    for target in "172.31.1.3 8000" "$postgres_ip 5432" "$caddy_ip 443" "1.1.1.1 443"; do
        # shellcheck disable=SC2086 # $target is "host port", two arguments.
        expect_fails "lab cannot reach $target" docker exec "$lab" python3 -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3)" $target
    done
else
    result 1 "lab of the restart scenario found"
fi
compose restart api >/dev/null
expect_ok "terminal closed by the restart and reopened on the same lab" wait "$restart_client"
cat "$work/restart.log"

echo "== logs"
for service in caddy api postgres; do
    expect_ok "$service logs rotate (json-file, 10m x 5)" \
        test "$(docker inspect --format '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} {{index .HostConfig.LogConfig.Config "max-file"}}' "$(compose ps -q $service)")" = "json-file 10m 5"
done
logs=$(compose logs 2>&1)
expect_fails "no invite code in any log" grep -qF "$invite" <<<"$logs"
expect_fails "no database password in any log" grep -qF "$(sed -n 's/^POSTGRES_PASSWORD=//p' "$env_file")" <<<"$logs"
expect_ok "Caddy access log redacts cookies" grep -q '"Cookie":\["REDACTED"\]' <<<"$logs"

echo "== cleanup by the API"
for _ in $(seq 60); do
    [ -z "$(docker ps -aq --filter label=linuxlab.deployment=$deployment)" ] && break
    sleep 1
done
expect_ok "every lab container of the run was removed by the API" \
    test -z "$(docker ps -aq --filter label=linuxlab.deployment=$deployment)"

if [ "$failures" -gt 0 ]; then
    echo "$failures checks failed" >&2
    exit 1
fi
echo "all checks passed"
