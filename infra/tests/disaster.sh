#!/usr/bin/env bash
# The loss of the host, end to end (docs/operations.md#disaster-recovery): two
# environments that share nothing but a MinIO server and the age keys of the run.
#
#   A, the origin: the production stack of RELEASE_FILE on this host's Docker Engine
#      (a disposable project, as infra/tests/e2e.sh), with synthetic missions and
#      students; backed up by migrate.sh and twice by backup.sh, encrypted, confirmed
#      in MinIO with the host's credential. Its facts are recorded (every table's
#      content, sequences, extensions, schema, Caddy's certificate and CA), then it
#      is destroyed: its containers, volumes, backups, receipts and settings.
#   B, the new host: a privileged container running its own Docker Engine, on a
#      Docker volume of its own: no image, volume, release file, backup or receipt of
#      A. It is prepared as a host is (packages, then age, rclone and gVisor with the
#      repository's installers), given only the new host's settings, the recovery
#      credential (list and read) and the recovery key, and recovered with
#      infra/ops/restore-offsite.sh from MinIO and clones of the two repositories.
#
# Then B is compared with A's facts, run again (nothing redone), refused where it must
# be, and used: the site's checks, a student of A logs in and finds their progress, a
# new student's whole session with a lab under runsc in B.
#
#   infra/tests/disaster.sh RELEASE_FILE --api-repo DIR --web-repo DIR [--runtime runsc|runc] [--keep]
#
# RELEASE_FILE is A's, built from the commits checked out in DIR (infra/release/build.sh);
# B gets the repositories as git bundles of those checkouts, which need their full
# history. When A is destroyed, RELEASE_FILE and its three images are removed too:
# they are A's. --runtime applies to A's labs only; B's are always under runsc. Needs, on
# this host: Docker, age, rclone, minio and mc (as e2e.sh), and network access from B
# (Alpine packages, the pinned releases). If this host has swap, B runs as a test
# project that may ignore it (the production project refuses unencrypted swap); CI
# turns swap off and B runs as linuxlab-prod. Every resource is named with the run's
# id and removed at the end unless --keep.
set -euo pipefail
docker() { MSYS_NO_PATHCONV=1 command docker "$@"; }

DIND_IMAGE=docker:28.4.0-dind@sha256:2ceb471176ad51e37145d43ce7cbf0fa5d644a2b185bd537f0ef695fb3a37497

release='' api_repo='' web_repo='' runtime=runsc keep=false
while [ $# -gt 0 ]; do
    case "$1" in
        --api-repo) api_repo=$2; shift 2 ;;
        --web-repo) web_repo=$2; shift 2 ;;
        --runtime) runtime=$2; shift 2 ;;
        --keep) keep=true; shift ;;
        -*) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
        *) release=$1; shift ;;
    esac
done
if [ ! -f "$release" ] || [ ! -d "$api_repo" ] || [ ! -d "$web_repo" ]; then
    echo "usage: disaster.sh RELEASE_FILE --api-repo DIR --web-repo DIR [--runtime runsc|runc] [--keep]" >&2
    exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
value() { sed -n "s/^$1=//p" "$release" | tail -n 1; }
api_image=$(value API_IMAGE)
api_sha=$(value API_SHA)
web_sha=$(value WEB_SHA)
for name in API LAB EDGE; do
    [ "$(docker image inspect --format '{{.Id}}' "$(value "${name}_IMAGE")")" = "$(value "${name}_IMAGE_ID")" ] \
        || { echo "error: $(value "${name}_IMAGE") is not the image recorded in $release" >&2; exit 1; }
done
[ "$(git -C "$api_repo" rev-parse HEAD)" = "$api_sha" ] || { echo "error: $api_repo is not at $api_sha" >&2; exit 1; }
[ "$(git -C "$web_repo" rev-parse HEAD)" = "$web_sha" ] || { echo "error: $web_repo is not at $web_sha" >&2; exit 1; }

run_id=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
network="linuxlab-dr-$run_id"
host_b="linuxlab-dr-host-$run_id"
volume_b="linuxlab-dr-docker-$run_id"
origin=linuxlab-dr-origin
deployment=dr-origin
work=$(mktemp -d)
chmod 700 "$work"
memory=$(mktemp -d /dev/shm/linuxlab-dr.XXXXXX)
MINIO_PID=''
verdict=failure

cleanup() {
    echo "Disaster Recovery E2E: $verdict"
    if [ "$keep" = true ]; then
        echo "kept: $host_b, $volume_b, $network, $work"
        return
    fi
    compose_a --profile tasks down --volumes --remove-orphans >/dev/null 2>&1 || true
    for id in $(docker ps -aq --filter "label=linuxlab.deployment=$deployment"); do docker rm -f "$id" >/dev/null; done
    docker rm -f "$host_b" >/dev/null 2>&1 || true
    docker volume rm "$volume_b" >/dev/null 2>&1 || true
    [ -z "$MINIO_PID" ] || kill "$MINIO_PID" 2>/dev/null || true
    docker network rm "$network" >/dev/null 2>&1 || true
    rm -rf "$work" "$memory"
}
trap cleanup EXIT

failures=0
result() {
    if [ "$1" = 0 ]; then echo "ok    $2"; else echo "FAIL  $2" >&2; failures=$((failures + 1)); fi
}
expect_ok() { local what=$1; shift; if "$@"; then result 0 "$what"; else result 1 "$what"; fi; }
expect_fails() { local what=$1; shift; if "$@" >/dev/null 2>&1; then result 1 "$what"; else result 0 "$what"; fi; }
same() { cmp -s "$1" "$2" || { diff "$1" "$2" | head -n 20 >&2; return 1; }; }
# The runner's memory at the points where it matters: A up, A gone, B recovered.
memory_note() {
    echo "memory  $1: $(free -m | awk '/^Mem:/ {print $7 " MiB available of " $2 " MiB"}')"
}
memory_note "start"

echo "== shared: MinIO and the keys of this run"
docker network create --subnet 172.29.9.0/24 --gateway 172.29.9.1 --label "linuxlab.test=disaster-$run_id" "$network" >/dev/null
mkdir -m 700 "$work/keys" "$work/conf"
for key in operator recovery; do age-keygen -o "$work/keys/$key.key" 2>/dev/null; done
{ age-keygen -y "$work/keys/operator.key"; age-keygen -y "$work/keys/recovery.key"; } >"$work/keys/recipients.txt"
# shellcheck source-path=SCRIPTDIR source=minio.sh
source "$here/minio.sh"
# On the network's gateway: this host (A) and B's container both reach it there.
minio_start "$work/minio" 172.29.9.1
minio_rclone_config "$work/conf/host.conf" "$MINIO_PORT" 172.29.9.1
minio_recovery_config "$work/conf/recovery.conf" "$MINIO_PORT" 172.29.9.1
echo "MinIO on 172.29.9.1:$MINIO_PORT, bucket backups, prefix host/"

# ---------------------------------------------------------------------------
echo "== A: the origin"
origin_dir="$work/origin"
mkdir -m 700 "$origin_dir"
random() { python3 -c "import secrets; print(secrets.token_hex($1))"; }
port=$(free_port)
invite_a=$(random 16)
env_a="$origin_dir/production.env"
(
    umask 077
    cat >"$env_a" <<EOF
SITE_ADDRESS=linuxlab.localhost
PUBLIC_ORIGIN=https://linuxlab.localhost:$port
POSTGRES_PASSWORD=$(random 32)
SIGNUP_INVITE_CODE=$invite_a
DOCKER_SOCKET_GID=$(docker run --rm -v /var/run/docker.sock:/var/run/docker.sock --entrypoint stat "$api_image" -c %g /var/run/docker.sock)
LAB_DEPLOYMENT=$deployment
LAB_CAPACITY=4
PUBLIC_SUBNET=172.28.0.0/24
EDGE_SUBNET=172.28.1.0/24
DB_SUBNET=172.28.2.0/24
CADDY_EDGE_IP=172.28.1.2
API_EDGE_IP=172.28.1.3
E2E_BROWSER_PORT=$port
E2E_RUNTIME=$runtime
E2E_ENVIRONMENT=$([ "$runtime" = runsc ] && echo production || echo development)
BACKUP_DIR=$origin_dir/backups
BACKUP_AGE_RECIPIENTS_FILE=$work/keys/recipients.txt
BACKUP_WORK_DIR=$memory/work
BACKUP_REMOTE=offsite:backups/host
BACKUP_RCLONE_CONFIG=$work/conf/host.conf
BACKUP_RETRIES=2
BACKUP_RETRY_DELAY=1
EOF
)
compose_a() {
    docker compose -p "$origin" -f "$repo/infra/compose.prod.yml" -f "$here/compose.e2e.yml" \
        --env-file "$release" --env-file "$env_a" "$@"
}
printf 'Filename\tType\tSize\tUsed\tPriority\n' >"$work/no-swaps"
export LINUXLAB_ENV_FILE=$env_a LINUXLAB_PROJECT=$origin LINUXLAB_TEST_LOCK_FILE=$work/ops.lock
export LINUXLAB_OVERRIDES=$here/compose.e2e.yml LINUXLAB_TEST_SWAPS=$work/no-swaps
content="$work/content-repo"
mkdir -p "$content"
cp -r "$repo/tests/fixtures/content" "$content/content"
git -C "$content" init -q
git -C "$content" config core.autocrlf false
git -C "$content" add content
git -C "$content" -c user.name=dr -c user.email=dr@dr.example commit -q -m content
content_sha=$(git -C "$content" rev-parse HEAD)
expect_ok "A: migrated, content synced at $content_sha, started" sh -c "
    '$repo/infra/ops/migrate.sh' '$release' >'$work/a-start.log' 2>&1 &&
    '$repo/infra/ops/content-sync.sh' '$release' '$content' '$content_sha' >>'$work/a-start.log' 2>&1"
compose_a up -d --wait >/dev/null
for _ in $(seq 30); do
    compose_a exec -T caddy cat /data/caddy/pki/authorities/local/root.crt >"$work/root-a.crt" 2>/dev/null && break
    sleep 1
done
cp "$here/e2e.py" "$work/e2e.py"
caddy_a=$(docker inspect --format "{{(index .NetworkSettings.Networks \"${origin}_public\").IPAddress}}" "$(compose_a ps -q caddy)")
client_a() {
    local address=$1
    shift
    mkdir -p "$work/client-a"
    cp "$work/e2e.py" "$work/client-a/e2e.py"
    cp "$work/root-a.crt" "$work/client-a/root.crt"
    docker run --rm --user "$(id -u):$(id -g)" --network "${origin}_public" --ip "$address" \
        --add-host "linuxlab.localhost:$caddy_a" -e E2E_HOST=linuxlab.localhost \
        -e "E2E_ORIGIN=https://linuxlab.localhost:$port" -e "E2E_INVITE=$invite_a" \
        -v "$work/client-a:/e2e:ro" --entrypoint python "$api_image" /e2e/e2e.py "$@"
}
end_a() { client_a 172.28.0.150 "$@"; }
# session ENDER LOG COMMAND...: a student's session (e2e.py), its output in $work/LOG.
# Only A's sessions, which make the data, get a second attempt, and only when the
# first stopped on the one known failure of the Python test client: its WebSocket
# handshake over TLS times out, rarely, on a healthy server (seen in e2e.sh as well).
# The first failure is shown; ENDER ends any lab it left. Any other failure, and every
# session and check on B, is not retried.
session() {
    local ender=$1 log=$2 argument
    shift 2
    "$@" >"$work/$log.1" 2>&1 && return 0
    if ! grep -q "timed out while waiting for handshake response" "$work/$log.1"; then
        tail -n 40 "$work/$log.1" >&2
        return 1
    fi
    echo "note  first attempt stopped on the test client's WebSocket handshake timeout:"
    grep -E '^(ok|FAIL)|TimeoutError' "$work/$log.1" | tail -n 4 | sed 's/^/        /'
    for argument in "$@"; do
        if [[ $argument == *@* ]]; then "$ender" end "$argument" >/dev/null 2>&1 || true; fi
    done
    "$@" >"$work/$log.2" 2>&1 && { echo "note  passed on the second attempt"; return 0; }
    tail -n 40 "$work/$log.2" >&2
    return 1
}
# logged LOG COMMAND...: COMMAND with its output in $work/LOG, shown when it fails.
logged() {
    "${@:2}" >"$work/$1" 2>&1 || { tail -n 40 "$work/$1" >&2; return 1; }
}
expect_ok "A: students ana and bia: lab, terminal, history, validation" \
    session end_a a-flow-1.log client_a 172.28.0.130 flow ana@dr.example bia@dr.example
first=$("$repo/infra/ops/backup.sh" "$release" before 2>"$work/a-backup-1.log") || true
expect_ok "A: a first backup, confirmed in MinIO" test -f "$first.receipt.json"
expect_ok "A: students caio and dani, after it" \
    session end_a a-flow-2.log client_a 172.28.0.131 flow caio@dr.example dani@dr.example

# No lab left unfinished: the database then stays as dumped once B's API starts.
psql_a() { compose_a exec -T postgres psql -X -At -U linuxlab -d linuxlab -c "$1"; }
unfinished() { psql_a "SELECT count(*) FROM lab_sessions WHERE status NOT IN ('terminated', 'failed')"; }
for _ in $(seq 120); do
    [ "$(unfinished)" = 0 ] && break
    sleep 1
done
expect_ok "A: every lab finished before the last backup" test "$(unfinished)" = 0
compose_a stop api >/dev/null

# Facts of a database: every table's rows (md5 of the sorted rows), the sequences, the
# extensions and the schema as pg_dump writes it (without its random \restrict key).
facts() {
    local psql=$1 dump=$2 out=$3
    "$psql" "
        SELECT tablename || ' ' || (xpath('/row/h/text()', query_to_xml(format(
                   'SELECT count(*) || '' '' || md5(coalesce(string_agg(x::text, %L ORDER BY x::text), %L)) AS h FROM public.%I x',
                   E'\n', '', tablename), false, true, '')))[1]::text
        FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename" >"$out.tables"
    "$psql" "SELECT sequencename || ' ' || coalesce(last_value::text, '-') FROM pg_sequences ORDER BY 1" >"$out.sequences"
    "$psql" "SELECT extname || ' ' || extversion FROM pg_extension ORDER BY 1" >"$out.extensions"
    "$psql" "SELECT coalesce(source_commit, '-') FROM content_syncs ORDER BY id DESC LIMIT 1" >"$out.content"
    "$dump" | sed '/^.restrict /d; /^.unrestrict /d' >"$out.schema"
}
schema_a() { compose_a exec -T postgres pg_dump -U linuxlab -d linuxlab --schema-only --no-owner; }
facts psql_a schema_a "$work/a"
certificate_sha() { python3 -c '
import base64, hashlib, re, sys
pem = sys.stdin.read()
body = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", pem, re.S).group(1)
print(hashlib.sha256(base64.b64decode("".join(body.split()))).hexdigest())'; }
compose_a exec -T caddy cat /data/caddy/certificates/local/linuxlab.localhost/linuxlab.localhost.crt | certificate_sha >"$work/a.certificate"
certificate_sha <"$work/root-a.crt" >"$work/a.root"
echo "A: $(wc -l <"$work/a.tables") tables ($(paste -sd ' ' "$work/a.tables" | cut -c1-200)...)"
last=$("$repo/infra/ops/backup.sh" "$release" disaster 2>"$work/a-backup-2.log") || true
if [ ! -f "$last.receipt.json" ]; then
    result 1 "A: the last backup, confirmed in MinIO, its receipt here"
    tail -n 20 "$work/a-backup-2.log" >&2
    exit 1
fi
result 0 "A: the last backup, confirmed in MinIO, its receipt here"
last_name=$(basename "$last")
last_key=${last_name#linuxlab-}
last_key=${last_key%.tar.age}
age --decrypt --identity "$work/keys/operator.key" --output "$memory/last.tar" "$last"
manifest_content() { python3 -c 'import json, sys, tarfile; m = json.load(tarfile.open(sys.argv[1]).extractfile("manifest.json")); print(m["code"]["content"]["sha"])' "$memory/last.tar"; }
expect_ok "A: its manifest names the content commit synced ($content_sha)" test "$(manifest_content)" = "$content_sha"
rm -f "$memory/last.tar"
# migrate.sh's backup (before the first migration), "before" and "disaster".
backups_a=$(find "$origin_dir/backups" -maxdepth 1 -name 'linuxlab-*.tar.age.receipt.json' | wc -l)
expect_ok "MinIO holds A's $backups_a backups, each a package, a checksum and a receipt" \
    test "$backups_a" = 3 -a "$(mc ls admin/backups/host/ | wc -l)" = 9

memory_note "A up, backed up"
# What must outlive A: MinIO and its objects, recorded with their SHA-256.
objects() { mc ls admin/backups/host/ | awk '{print $NF}' | sort; }
object_sums() {
    local name
    for name in $(objects); do echo "$name $(mc cat "admin/backups/host/$name" | sha256sum | cut -c1-64)"; done
}
object_sums >"$work/objects-before"
echo "== A is lost"
images_a=("$(value API_IMAGE)" "$(value LAB_IMAGE)" "$(value EDGE_IMAGE)")
ids_a=("$(value API_IMAGE_ID)" "$(value LAB_IMAGE_ID)" "$(value EDGE_IMAGE_ID)")
compose_a --profile tasks down --volumes --remove-orphans >/dev/null 2>&1
for id in $(docker ps -aq --filter "label=linuxlab.deployment=$deployment"); do docker rm -f "$id" >/dev/null; done
# A's images, by the tags and ids its release file recorded: built for this run only.
docker image rm "${images_a[@]}" >/dev/null 2>&1 || true
for id in "${ids_a[@]}"; do docker image rm "$id" >/dev/null 2>&1 || true; done
# A's settings, backups, receipts, host credential and content checkout. The keys and
# the recovery credential stay: they are what the operator brings to the new host.
rm -rf "$origin_dir" "$work/client-a" "$work/root-a.crt" "$memory/work" "$work/ops.lock" "$work/no-swaps" \
    "$work/conf/host.conf" "$content"
rm -f "$release"
unset LINUXLAB_ENV_FILE LINUXLAB_PROJECT LINUXLAB_TEST_LOCK_FILE LINUXLAB_OVERRIDES LINUXLAB_TEST_SWAPS
# Each condition on its own, so that a failure names what is left.
failures_before=$failures
none() { test -z "$("$@")"; }
image_gone() { ! docker image inspect "$1" >/dev/null 2>&1; }
expect_ok "A gone: no container of its project" none docker ps -aq --filter "label=com.docker.compose.project=$origin"
expect_ok "A gone: no lab container of its deployment" none docker ps -aq --filter "label=linuxlab.deployment=$deployment"
expect_ok "A gone: no volume of its project" none docker volume ls -q --filter "label=com.docker.compose.project=$origin"
expect_ok "A gone: no network of its project" none docker network ls -q --filter "label=com.docker.compose.project=$origin"
for index in 0 1 2; do
    expect_ok "A gone: image ${images_a[index]} (${ids_a[index]:7:12})" image_gone "${ids_a[index]}"
done
expect_ok "A gone: its release file" test ! -e "$release"
expect_ok "A gone: its settings, backup directory, backups, checksums and receipts" test ! -e "$origin_dir"
expect_ok "A gone: its credential for the destination" test ! -e "$work/conf/host.conf"
expect_ok "A gone: its content checkout" test ! -e "$content"
expect_ok "A gone: nothing in its work directory (memory)" none find "$memory" -mindepth 1
# Anywhere in this run's files but MinIO's own data directory, which holds the
# objects A uploaded (as directories named after them) and must stay.
local_copies() {
    find "$work" "$memory" -path "$work/minio/data" -prune -o \( -name 'linuxlab-*.tar.age*' \
        -o -name 'release-*.env' -o -name production.env \) -print
}
expect_ok "A gone: no package, checksum, receipt, release file or settings left outside MinIO's data" none local_copies
# The search of the first version of this check, which did not leave MinIO's data out
# and failed with nothing of A left: what it finds, for the record.
echo "note  the same search without leaving MinIO's data out finds $(find "$work" "$memory" \
    \( -name 'linuxlab-*.tar.age*' -o -name 'release-*.env' -o -name production.env \) -print \
    | sed "s#^$work/##" | sort | paste -sd ' ' | cut -c1-400)"
if [ "$failures" = "$failures_before" ]; then
    result 0 "A is gone: every local resource of its platform"
else
    result 1 "A is gone: every local resource of its platform ($((failures - failures_before)) left, above)"
fi
minio_ready() { curl -fsS "http://172.29.9.1:$MINIO_PORT/minio/health/ready" >/dev/null; }
expect_ok "MinIO kept: it answers" minio_ready
object_sums >"$work/objects-after"
expect_ok "MinIO kept: A's $(wc -l <"$work/objects-before") objects (packages, checksums, receipts), same bytes" \
    same "$work/objects-before" "$work/objects-after"
memory_note "A gone"

# ---------------------------------------------------------------------------
echo "== B: a new host, its own Docker Engine"
docker volume create --label "linuxlab.test=disaster-$run_id" "$volume_b" >/dev/null
docker run -d --privileged --name "$host_b" --hostname recovered --network "$network" --ip 172.29.9.10 \
    -v "$volume_b:/var/lib/docker" --tmpfs /run:size=256m,mode=755 \
    --tmpfs /mnt/linuxlab-work:size=1g,mode=700 --label "linuxlab.test=disaster-$run_id" \
    --entrypoint sleep "$DIND_IMAGE" infinity >/dev/null
in_b() { docker exec -i "$host_b" "$@"; }
mounts_b=$(docker inspect --format '{{range .Mounts}}{{.Type}}:{{.Name}}{{.Source}} {{end}}' "$host_b")
expect_ok "B mounts nothing of this host but its own Docker volume ($mounts_b)" \
    test "$mounts_b" = "volume:$volume_b$(docker volume inspect --format '{{.Mountpoint}}' "$volume_b") "
# Preparing the host: packages, the source code, then the repository's installers.
in_b apk add --no-cache bash coreutils util-linux findutils python3 git curl bzip2 tar gzip grep sed >"$work/b-prepare.log" 2>&1
git -C "$api_repo" bundle create "$work/api.bundle" --all 2>/dev/null
git -C "$web_repo" bundle create "$work/web.bundle" --all 2>/dev/null
docker cp "$work/api.bundle" "$host_b:/root/api.bundle" >/dev/null
docker cp "$work/web.bundle" "$host_b:/root/web.bundle" >/dev/null
rm -f "$work/api.bundle" "$work/web.bundle"
in_b bash -c "
    set -e
    git clone -q /root/api.bundle /srv/src/linux-lab-api && git -C /srv/src/linux-lab-api checkout -q --detach $api_sha
    git clone -q /root/web.bundle /srv/src/linux-lab-web && git -C /srv/src/linux-lab-web checkout -q --detach $web_sha
    rm /root/api.bundle /root/web.bundle
    cd /srv/src/linux-lab-api
    infra/release/install-age.sh /usr/local/bin
    infra/release/install-rclone.sh /usr/local/bin
    infra/release/install-gvisor.sh /usr/local/bin
    (nohup dind dockerd >/var/log/dockerd.log 2>&1 &)
    for _ in \$(seq 60); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done
    exit 1" >>"$work/b-prepare.log" 2>&1 \
    || { tail -n 30 "$work/b-prepare.log" >&2; result 1 "B prepared"; exit 1; }
result 0 "B prepared: age, rclone and gVisor installed by the repository's installers, its Docker Engine started"
expect_ok "B's engine: no image, no volume, no container" \
    test -z "$(in_b docker images -q)$(in_b docker volume ls -q)$(in_b docker ps -aq)"
expect_ok "B has no release file, backup or receipt" \
    test -z "$(in_b find / -xdev \( -name 'release-*.env' -o -name 'linuxlab-*.tar.age*' \) -print 2>/dev/null)"
# shellcheck disable=SC2016 # A Go template, not shell.
runtimes_b() { in_b docker info --format '{{range $name, $_ := .Runtimes}}{{$name}} {{end}}'; }
expect_ok "B's engine has the runsc runtime" grep -qw runsc <<<"$(runtimes_b)"

# The new host's settings, given by the operator: new secrets, nothing of A's.
project_b=linuxlab-prod
extra_b=()
if [ "$(sed 1d /proc/swaps | wc -l)" != 0 ]; then
    echo "note  this host has swap ($(sed 1d /proc/swaps | awk '{print $1}' | paste -sd ' ')), which B shares: B runs as a test project that ignores it"
    project_b=linuxlab-dr-recovered
    in_b sh -c 'printf "Filename\tType\tSize\tUsed\tPriority\n" >/root/no-swaps'
    extra_b=("LINUXLAB_PROJECT=$project_b" LINUXLAB_TEST_SWAPS=/root/no-swaps)
fi
invite_b=$(random 16)
password_b=$(random 32)
gid_b=$(in_b stat -c %g /var/run/docker.sock)
in_b sh -c 'install -d -m 700 /etc/linuxlab /run/linuxlab-key'
in_b sh -c 'umask 077; cat >/etc/linuxlab/production.env' <<EOF
SITE_ADDRESS=linuxlab.localhost
PUBLIC_ORIGIN=https://linuxlab.localhost
POSTGRES_PASSWORD=$password_b
SIGNUP_INVITE_CODE=$invite_b
DOCKER_SOCKET_GID=$gid_b
LAB_DEPLOYMENT=recovered
LAB_CAPACITY=4
BACKUP_AGE_RECIPIENTS_FILE=/etc/linuxlab/backup-recipients.txt
BACKUP_WORK_DIR=/mnt/linuxlab-work
EOF
in_b sh -c 'umask 022; cat >/etc/linuxlab/backup-recipients.txt' <"$work/keys/recipients.txt"
in_b sh -c 'umask 077; cat >/etc/linuxlab/recovery-rclone.conf' <"$work/conf/recovery.conf"
in_b sh -c 'umask 077; cat >/run/linuxlab-key/recovery.key' <"$work/keys/recovery.key"
recover_b() {
    in_b env "${extra_b[@]}" "$@" bash -c 'cd /srv/src/linux-lab-api && infra/ops/restore-offsite.sh \
        --remote recovery:backups/host --rclone-config /etc/linuxlab/recovery-rclone.conf \
        --identity-file /run/linuxlab-key/recovery.key \
        --api-repo /srv/src/linux-lab-api --web-repo /srv/src/linux-lab-web'
}

echo "== B: recovery"
started=$(date +%s)
if recover_b >"$work/recovery.log" 2>&1; then
    result 0 "restore-offsite.sh: recovered in $(($(date +%s) - started)) s"
else
    result 1 "restore-offsite.sh: recovered"
    tail -n 60 "$work/recovery.log" >&2
    exit 1
fi
sed 's/^/    /' "$work/recovery.log" | grep -E '^    (==|.*(selected|valid receipt|images built|matches the manifest|restored into|missions:|API:|Caddy:|ok |note )|backup:|api:|web:|content:)' || true
expect_ok "the newest backup was found and used ($last_key)" grep -q "^backup: $last_key " "$work/recovery.log"
expect_ok "  found by listing with the recovery credential, not from a file of A" grep -q "$backups_a valid receipt(s) of $backups_a" "$work/recovery.log"

release_b=$(in_b sh -c 'ls /var/lib/linuxlab/releases/release-*.env')
compose_b() {
    in_b docker compose -p "$project_b" -f /srv/src/linux-lab-api/infra/compose.prod.yml \
        --env-file "$release_b" --env-file /etc/linuxlab/production.env "$@"
}
psql_b() { compose_b exec -T postgres psql -X -At -U linuxlab -d linuxlab -c "$1"; }
schema_b() { compose_b exec -T postgres pg_dump -U linuxlab -d linuxlab --schema-only --no-owner; }
facts psql_b schema_b "$work/b"
expect_ok "every table's rows are A's ($(wc -l <"$work/a.tables") tables, row counts and content)" same "$work/a.tables" "$work/b.tables"
expect_ok "the sequences are A's" same "$work/a.sequences" "$work/b.sequences"
expect_ok "the extensions are A's" same "$work/a.extensions" "$work/b.extensions"
expect_ok "the schema is A's (pg_dump --schema-only)" same "$work/a.schema" "$work/b.schema"
expect_ok "the last content sync is A's, commit $content_sha" test "$(cat "$work/b.content")" = "$content_sha"
expect_ok "  and the report names it" grep -q "^content: $content_sha" "$work/recovery.log"
label() { in_b docker image inspect --format "{{index .Config.Labels \"org.opencontainers.image.revision\"}}" "$1"; }
image_b() { sed -n "s/^$1=//p" <(in_b cat "$release_b"); }
expect_ok "B's images were built here, labeled with the manifest's commits" \
    test "$(label "$(image_b API_IMAGE)") $(label "$(image_b LAB_IMAGE)") $(label "$(image_b EDGE_IMAGE)")" = "$api_sha $api_sha $web_sha"
caddy_b() { compose_b exec -T caddy cat "$1"; }
caddy_b /data/caddy/certificates/local/linuxlab.localhost/linuxlab.localhost.crt | certificate_sha >"$work/b.certificate"
caddy_b /data/caddy/pki/authorities/local/root.crt >"$work/root-b.crt"
certificate_sha <"$work/root-b.crt" >"$work/b.root"
expect_ok "Caddy's certificate in B is A's" same "$work/a.certificate" "$work/b.certificate"
expect_ok "Caddy's CA in B is A's" same "$work/a.root" "$work/b.root"
expect_ok "  and B's Caddy serves it (restore-offsite.sh compared it)" grep -q "ok    the certificate served is the restored one" "$work/recovery.log"
expect_ok "B's caddy-config started empty: only what Caddy wrote at its start" \
    test "$(in_b docker run --rm -v "${project_b}_caddy-config:/c:ro" --entrypoint find "$(image_b API_IMAGE)" /c -type f | sort | paste -sd ' ')" = "/c/caddy/autosave.json"

echo "== B: running the recovery again"
facts psql_b schema_b "$work/b-before-again"
if recover_b >"$work/recovery-again.log" 2>&1; then result 0 "a second run succeeds"; else result 1 "a second run succeeds"; tail -n 30 "$work/recovery-again.log" >&2; fi
expect_ok "  it restored nothing again" grep -q "was restored by an earlier run of this recovery: checked again" "$work/recovery-again.log"
facts psql_b schema_b "$work/b-after-again"
expect_ok "  and changed no row" same "$work/b-before-again.tables" "$work/b-after-again.tables"
in_b docker volume create linuxlab-elsewhere_postgres-data >/dev/null
if recover_b LINUXLAB_PROJECT=linuxlab-elsewhere >"$work/recovery-elsewhere.log" 2>&1; then
    result 1 "an existing volume without the recovery's label is refused"
else
    expect_ok "an existing volume without the recovery's label is refused, untouched" \
        grep -q "volume linuxlab-elsewhere_postgres-data exists and was not made by a recovery" "$work/recovery-elsewhere.log"
fi
expect_ok "  no other volume was made for it" \
    test "$(in_b docker volume ls -q --filter name=linuxlab-elsewhere)" = linuxlab-elsewhere_postgres-data
in_b docker volume rm linuxlab-elsewhere_postgres-data >/dev/null
expect_ok "the restore left nothing decrypted in B's work directory" test -z "$(in_b find /mnt/linuxlab-work -mindepth 1)"
expect_ok "B's downloads are encrypted only" \
    test -z "$(in_b find /var/lib/linuxlab/recovery -type f ! -name '*.tar.age' ! -name '*.sha256' ! -name receipt.json ! -name report.txt ! -name site.log ! -name 'done.*' ! -name 'restored.*')"

echo "== B: the platform"
caddy_ip_b=$(in_b docker inspect --format "{{(index .NetworkSettings.Networks \"${project_b}_public\").IPAddress}}" "$(compose_b ps -q caddy)")
in_b mkdir -p /srv/e2e
docker cp "$work/e2e.py" "$host_b:/srv/e2e/e2e.py" >/dev/null
docker cp "$work/root-b.crt" "$host_b:/srv/e2e/root.crt" >/dev/null
client_b() {
    in_b docker run --rm --network "${project_b}_public" --add-host "linuxlab.localhost:$caddy_ip_b" \
        -e E2E_HOST=linuxlab.localhost -e E2E_ORIGIN=https://linuxlab.localhost -e "E2E_INVITE=$invite_b" \
        -v /srv/e2e:/e2e:ro --entrypoint python "$(image_b API_IMAGE)" /e2e/e2e.py "$@"
}
memory_note "B recovered"
expect_ok "the site through B's Caddy: headers, routes, API, limits, redirect" logged b-site.log client_b site
expect_ok "ana, a student of A, logs in on B and finds sample-file completed; a lab of it starts under runsc" \
    logged b-returning.log client_b returning ana@dr.example sample-file
expect_ok "a new student's whole session on B: lab, terminal, validation, history, reset, switch" \
    logged b-flow.log client_b flow erin@dr.example fabi@dr.example
expect_ok "B's API runs labs under runsc, in production mode" \
    test "$(compose_b exec -T api printenv LAB_OCI_RUNTIME ENVIRONMENT | paste -sd ' ')" = "runsc production"
expect_ok "no private key in any log of the run" sh -c "! grep -rq AGE-SECRET-KEY '$work'/*.log"
expect_ok "neither A's nor B's database password in the recovery's log" \
    sh -c "! grep -qF '$password_b' '$work/recovery.log'"

a_absent() {
    local id
    for id in "${ids_a[@]}"; do image_gone "$id" || return 1; done
    none docker ps -aq --filter "label=com.docker.compose.project=$origin" \
        && none docker volume ls -q --filter "label=com.docker.compose.project=$origin" \
        && none local_copies && test ! -e "$release" && test ! -e "$origin_dir"
}
expect_ok "at the end, still nothing of A on this host: images, containers, volumes, files" a_absent
memory_note "end"
if [ "$failures" -gt 0 ]; then
    echo "$failures checks failed" >&2
    exit 1
fi
echo "all checks passed"
verdict=success
