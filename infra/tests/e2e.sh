#!/usr/bin/env bash
# End-to-end checks of the production stack (docs/deployment.md#checking-a-release):
# starts infra/compose.prod.yml with the release's images as a disposable project,
# with the synthetic missions of tests/fixtures/content, and checks it from clients
# on the entry's outer network, each with its own address, through the entry only.
#
#   infra/tests/e2e.sh RELEASE_FILE [--entry direct|ngrok] [--runtime runsc|runc] [--keep]
#
# --entry direct (the default) is infra/caddy/Caddyfile: clients reach Caddy on the
# public network. --entry ngrok adds infra/compose.ngrok.yml with a stand-in for the
# agent (compose.ngrok-standin.yml): clients reach the stand-in on the agent's egress
# network, and it forwards to Caddy over the tunnel network as ngrok does.
# --runtime runc is for hosts without gVisor (Docker Desktop); it also runs the API
# with ENVIRONMENT=development, which is what allows runc. Everything is removed at
# the end unless --keep. Never run it with the production project name.
set -euo pipefail
# Git Bash on Windows rewrites arguments that look like paths: container paths
# must reach docker as written, and host paths go through host_path.
docker() { MSYS_NO_PATHCONV=1 command docker "$@"; }

release='' entry=direct runtime=runsc keep=false
while [ $# -gt 0 ]; do
    case "$1" in
        --entry) entry=$2; shift 2 ;;
        --runtime) runtime=$2; shift 2 ;;
        --keep) keep=true; shift ;;
        -*) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
        *) release=$1; shift ;;
    esac
done
[ -f "$release" ] || { echo "usage: e2e.sh RELEASE_FILE [--entry direct|ngrok] [--runtime runsc|runc] [--keep]" >&2; exit 2; }
case "$entry" in direct | ngrok) ;; *) echo "error: --entry is direct or ngrok" >&2; exit 2 ;; esac

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
EDGE_SUBNET=172.31.1.0/24
DB_SUBNET=172.31.2.0/24
CADDY_EDGE_IP=172.31.1.2
API_EDGE_IP=172.31.1.3
E2E_RUNTIME=$runtime
E2E_ENVIRONMENT=$([ "$runtime" = runsc ] && echo production || echo development)
BACKUP_DIR=$work/backups
BACKUP_KEEP_DAYS=1
EOF
    # Clients are always on 172.31.0.0/24: the public network, or the agent's egress.
    if [ "$entry" = direct ]; then
        echo PUBLIC_SUBNET=172.31.0.0/24
    else
        printf '%s\n' PUBLIC_SUBNET=172.31.4.0/24 NGROK_EGRESS_SUBNET=172.31.0.0/24 \
            TUNNEL_SUBNET=172.31.3.0/24 CADDY_TUNNEL_IP=172.31.3.2 NGROK_AGENT_IP=172.31.3.3 \
            NGROK_URL=https://linuxlab.localhost "NGROK_ENV_FILE=$(host_path "$work/ngrok.env")"
        # The stand-in needs no authtoken; the file only has to exist.
        echo "# no authtoken: the agent is replaced by a stand-in" >"$work/ngrok.env"
    fi >>"$env_file"
)

# The deployment's files, then the test's.
deployment_files=("$repo/infra/compose.prod.yml")
test_files=("$here/compose.e2e.yml")
if [ "$entry" = ngrok ]; then
    deployment_files+=("$repo/infra/compose.ngrok.yml")
    test_files+=("$here/compose.ngrok-standin.yml")
fi
compose_files=()
for file in "${deployment_files[@]}"; do compose_files+=(-f "$(host_path "$file")"); done
compose_files+=(--env-file "$(host_path "$release")" --env-file "$(host_path "$env_file")")
test_args=()
for file in "${test_files[@]}"; do test_args+=(-f "$(host_path "$file")"); done
compose() {
    docker compose -p "$project" "${compose_files[@]}" "${test_args[@]}" "$@"
}

cleanup() {
    if [ "$keep" = false ]; then
        compose --profile tasks down --volumes --remove-orphans >/dev/null 2>&1 || true
        # Labs of this disposable deployment only.
        for id in $(docker ps -aq --filter label=linuxlab.deployment=$deployment); do docker rm -f "$id" >/dev/null; done
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
    docker run -i --rm --network none -e ENTRY="$entry" --entrypoint python "$api_image" -c '
import json, os, sys
config = json.load(sys.stdin)
services, networks = config["services"], config["networks"]
entry = os.environ["ENTRY"]
problems = []
for name, service in services.items():
    if service.get("ports") and (entry == "ngrok" or name != "caddy"):
        problems.append(f"{name} publishes ports")
    if "WEB_CONCURRENCY" in (service.get("environment") or {}):
        problems.append(f"{name} sets WEB_CONCURRENCY")
if entry == "direct":
    ports = sorted(port["target"] for port in services["caddy"]["ports"])
    if ports != [80, 443]:
        problems.append(f"caddy publishes {ports}")
    if sorted(services["caddy"]["networks"]) != ["edge", "public"]:
        problems.append("caddy networks")
else:
    caddy, agent = services["caddy"], services["ngrok"]
    if sorted(caddy["networks"]) != ["edge", "tunnel"]:
        problems.append("caddy networks")
    if sorted(agent["networks"]) != ["ngrok-egress", "tunnel"] or not networks["tunnel"].get("internal"):
        problems.append("agent networks")
    if "/etc/caddy/Caddyfile.ngrok" not in caddy["command"]:
        problems.append("caddy does not use the tunnel entry")
    if caddy["environment"]["NGROK_AGENT_IP"] != agent["networks"]["tunnel"]["ipv4_address"]:
        problems.append("caddy trusts an address that is not the agent")
    if "NGROK_AUTHTOKEN" in (agent.get("environment") or {}):
        problems.append("authtoken in the compose file instead of the environment file")
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
expect_ok "deployment compose ($entry): published ports, one API process trusting only Caddy, internal networks, socket only in the API" \
    check_config

echo "== start, with the operation scripts"
overrides=("${deployment_files[@]:1}" "${test_files[@]}")
export LINUXLAB_ENV_FILE=$env_file LINUXLAB_PROJECT=$project
LINUXLAB_OVERRIDES=$(IFS=:; echo "${overrides[*]}")
export LINUXLAB_OVERRIDES
# The synthetic missions, committed in a throwaway repository like the private one.
content="$work/content-repo"
mkdir -p "$content"
cp -r "$repo/tests/fixtures/content" "$content/content"
git -C "$content" init -q
git -C "$content" config core.autocrlf false
git -C "$content" add content
git -C "$content" -c user.name=e2e -c user.email=e2e@e2e.example commit -q -m content
content_sha=$(git -C "$content" rev-parse HEAD)

expect_ok "migrate.sh: backup, restore test, then upgrade from an empty database" \
    "$repo/infra/ops/migrate.sh" "$release"
expect_ok "migrate.sh again: nothing to migrate" "$repo/infra/ops/migrate.sh" "$release"
compose run --rm --no-deps -T task alembic downgrade -1 >/dev/null 2>&1
expect_ok "migrate.sh on a database one revision behind: upgraded to the head" \
    "$repo/infra/ops/migrate.sh" "$release"
expect_fails "content-sync.sh refuses a checkout at another commit" \
    "$repo/infra/ops/content-sync.sh" "$release" "$content" "$(printf '0%.0s' $(seq 40))"
touch "$content/content/untracked.txt"
expect_fails "content-sync.sh refuses untracked files" \
    "$repo/infra/ops/content-sync.sh" "$release" "$content" "$content_sha"
rm "$content/content/untracked.txt"
expect_ok "content-sync.sh: synthetic missions synced from the checkout" \
    "$repo/infra/ops/content-sync.sh" "$release" "$content" "$content_sha"
expect_ok "no container kept the content mount" \
    test -z "$(docker ps -aq --filter "label=com.docker.compose.project=$project" --filter volume=/content)"
compose up -d --wait >/dev/null
echo "stack up ($entry entry, $runtime)"

# What clients connect to: Caddy on the public network, or the agent on its egress.
if [ "$entry" = direct ]; then
    entry_service=caddy network="${project}_public"
else
    entry_service=ngrok network="${project}_ngrok-egress"
fi
for _ in $(seq 30); do
    compose exec -T "$entry_service" cat /data/caddy/pki/authorities/local/root.crt >"$work/root.crt" 2>/dev/null && break
    sleep 1
done
cp "$here/e2e.py" "$work/e2e.py"
entry_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"$network\").IPAddress}}" "$(compose ps -q "$entry_service")")
postgres_ip=$(docker inspect --format "{{(index .NetworkSettings.Networks \"${project}_db\").IPAddress}}" "$(compose ps -q postgres)")

client() {
    local address=$1
    shift
    docker run --rm --user "$(id -u):$(id -g)" --network "$network" --ip "$address" --add-host "linuxlab.localhost:$entry_ip" \
        -e E2E_ENTRY="$entry" -e E2E_HOST=linuxlab.localhost -e E2E_ORIGIN=https://linuxlab.localhost:8443 -e E2E_INVITE="$invite" \
        -v "$(host_path "$work"):/e2e:ro" --entrypoint python "$api_image" /e2e/e2e.py "$@"
}

echo "== site through the $entry entry"
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
expect_ok "clients reach the entry on 443 (control)" client 172.31.0.120 connect "$entry_ip" 443
expect_fails "clients cannot resolve the API" client 172.31.0.120 connect api 8000
expect_fails "clients cannot reach the API's address" client 172.31.0.120 connect 172.31.1.3 8000
expect_fails "clients cannot reach PostgreSQL" client 172.31.0.120 connect "$postgres_ip" 5432
expect_fails "clients cannot reach the entry's admin endpoint" client 172.31.0.120 connect "$entry_ip" 2019
if [ "$entry" = ngrok ]; then
    expect_fails "clients cannot resolve Caddy" client 172.31.0.120 connect caddy 80
    expect_fails "clients cannot reach Caddy's tunnel address" client 172.31.0.120 connect 172.31.3.2 80
    expect_fails "clients cannot reach Caddy's edge address" client 172.31.0.120 connect 172.31.1.2 80
    in_caddy() { compose exec -T caddy wget -q -T 3 -O /dev/null "$1"; }
    expect_fails "Caddy has no route to the internet in this entry" in_caddy http://1.1.1.1/
fi
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
    for target in "172.31.1.3 8000" "$postgres_ip 5432" "172.31.1.2 443" "1.1.1.1 443"; do
        # shellcheck disable=SC2086 # $target is "host port", two arguments.
        expect_fails "lab cannot reach $target" docker exec "$lab" python3 -c "import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3)" $target
    done
else
    result 1 "lab of the restart scenario found"
fi
compose restart api >/dev/null
# The checks below expect a healthy API, which takes a health check after a restart.
for _ in $(seq 60); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q api)")" = healthy ] && break
    sleep 1
done
expect_ok "terminal closed by the restart and reopened on the same lab" wait "$restart_client"
cat "$work/restart.log"

echo "== logs"
services=(caddy api postgres)
[ "$entry" = direct ] || services+=(ngrok)
for service in "${services[@]}"; do
    expect_ok "$service logs rotate (json-file, 10m x 5)" \
        test "$(docker inspect --format '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} {{index .HostConfig.LogConfig.Config "max-file"}}' "$(compose ps -q "$service")")" = "json-file 10m 5"
done
logs=$(compose logs 2>&1)
expect_fails "no invite code in any log" grep -qF "$invite" <<<"$logs"
expect_fails "no database password in any log" grep -qF "$(sed -n 's/^POSTGRES_PASSWORD=//p' "$env_file")" <<<"$logs"
expect_ok "Caddy access log redacts cookies" grep -q '"Cookie":\["REDACTED"\]' <<<"$logs"

echo "== backups"
dump=$("$repo/infra/ops/backup.sh" "$release" e2e 2>"$work/backup.log")
expect_ok "backup.sh wrote a readable dump and Caddy's data" \
    test -s "$dump" -a -s "$(ls "$work"/backups/caddy-data-*-e2e.tar.gz)"
if [ "$(uname -s)" = Linux ]; then
    expect_ok "backups are readable only by their owner" \
        test "$(stat -c %a "$work/backups")" = 700 -a "$(stat -c %a "$dump")" = 600
else
    echo "skip  backup file modes: not checkable on $(uname -s)"
fi
expect_ok "backup.sh reported that nothing was copied off the host" \
    grep -q "BACKUP_COPY_COMMAND not set" "$work/backup.log"
users_before=$(compose exec -T postgres psql -At -U linuxlab -d linuxlab -c 'SELECT count(*) FROM users')
expect_ok "restore.sh: restored into a new database and dropped it" \
    "$repo/infra/ops/restore.sh" "$release" "$dump"
expect_ok "the database in use was not touched ($users_before users)" \
    test "$(compose exec -T postgres psql -At -U linuxlab -d linuxlab -c 'SELECT count(*) FROM users')" = "$users_before"
expect_ok "no restore database left" \
    test "$(compose exec -T postgres psql -At -U linuxlab -d postgres -c "SELECT count(*) FROM pg_database WHERE datname LIKE 'restore_%'")" = 0
printf 'corrupt' >>"$dump"
expect_fails "restore.sh refuses a dump that does not match its checksum" \
    "$repo/infra/ops/restore.sh" "$release" "$dump"

echo "== lab checks"
for _ in $(seq 60); do
    [ -z "$(docker ps -aq --filter label=linuxlab.deployment=$deployment)" ] && break
    sleep 1
done
if "$repo/infra/ops/check-labs.sh" "$release" >"$work/check-labs-clean.log" 2>&1; then
    result 0 "check-labs.sh: nothing to report"
else
    result 1 "check-labs.sh: nothing to report"
    cat "$work/check-labs-clean.log"
fi
# check-labs.sh and the reaper are checked apart. The reaper (in the API) removes any
# orphan it recognizes on its next pass, so with the API running an orphan created
# for check-labs.sh could be gone before the report. With the API stopped no pass
# can run: the report sees both containers, and must leave both in place.
lab_image=$(value LAB_IMAGE)
orphan_key=$(random 16)
compose stop api >/dev/null
docker run -d --name "ll-lab-$orphan_key" --network none --label linuxlab.managed=true \
    --label linuxlab.lab_id="$orphan_key" --label linuxlab.deployment=$deployment "$lab_image" >/dev/null
docker run -d --name "e2e-unidentified" --network none \
    --label linuxlab.deployment=$deployment "$lab_image" >/dev/null
"$repo/infra/ops/check-labs.sh" "$release" >"$work/check-labs.log" 2>&1 && checked=0 || checked=$?
findings=$(grep -c -E 'ATTENTION  (orphan container ll-lab-|unidentified container e2e-unidentified|the API is not running and healthy)' "$work/check-labs.log" || true)
expect_ok "check-labs.sh reports the orphan, the unidentified container and the stopped API, exit 1" \
    test "$checked" = 1 -a "$findings" = 3 -a "$(grep -c ATTENTION "$work/check-labs.log")" = 3
exists() { docker inspect "$1" >/dev/null 2>&1; }
expect_ok "check-labs.sh removed neither container" exists "ll-lab-$orphan_key"
expect_ok "check-labs.sh left the unidentified container alone" exists e2e-unidentified

# Blocks until Docker reports the container removed (Engine API wait with
# condition=removed); a container already gone answers 404. Fails after $2 seconds.
wait_removed() {
    docker run --rm --network none --user 0 -v /var/run/docker.sock:/var/run/docker.sock \
        --entrypoint python "$api_image" -c '
import http.client, socket, sys
class Docker(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(float(sys.argv[2]))
        self.sock.connect("/var/run/docker.sock")
connection = Docker("docker")
connection.request("POST", f"/containers/{sys.argv[1]}/wait?condition=removed")
response = connection.getresponse()
response.read()
sys.exit(0 if response.status in (200, 404) else 1)' "$1" "$2"
}
# The reaper's first pass runs when the API starts.
compose up -d --wait --no-recreate api >/dev/null
expect_ok "the reaper's startup pass removed the orphan" wait_removed "ll-lab-$orphan_key" 120
expect_ok "the reaper left the unidentified container alone" exists e2e-unidentified
docker rm -f e2e-unidentified >/dev/null

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
