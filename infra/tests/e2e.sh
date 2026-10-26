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
# Git Bash on Windows rewrites arguments that look like paths: container paths
# must reach docker as written, and host paths go through host_path.
docker() { MSYS_NO_PATHCONV=1 command docker "$@"; }

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
BACKUP_DIR=$work/backups
BACKUP_KEEP_DAYS=1
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

echo "== start, with the operation scripts"
export LINUXLAB_ENV_FILE=$env_file LINUXLAB_PROJECT=$project LINUXLAB_OVERRIDES="$here/compose.e2e.yml"
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
    docker run --rm --user "$(id -u):$(id -g)" --network "$network" --ip "$address" --add-host "linuxlab.localhost:$caddy_ip" \
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
# The checks below expect a healthy API, which takes a health check after a restart.
for _ in $(seq 60); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q api)")" = healthy ] && break
    sleep 1
done
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
