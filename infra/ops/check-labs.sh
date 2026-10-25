#!/usr/bin/env bash
# Compares this deployment's lab containers with the labs PostgreSQL records, and
# reports what the reaper has not settled, plus disk, memory and errors logged by
# the API. It never removes or changes anything: the reaper does that, and a
# container that is not plainly this deployment's is left for a person to look at.
# See docs/operations.md#checking-labs.
#
#   infra/ops/check-labs.sh RELEASE_FILE
#
# Exit status 0 when nothing needs attention, 1 otherwise (for cron).
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

load_release "${1:-}"
deployment=$(setting LAB_DEPLOYMENT)
[ -n "$deployment" ] || fail "LAB_DEPLOYMENT is not set"
capacity=$(setting LAB_CAPACITY)
database=$(database_name)

# A lab that ended or failed is finished by the reaper within a minute plus a pass
# (30 s); provisioning gives up after 2 minutes. These leave room for both.
STUCK_ENDING_SECONDS=180
STUCK_PROVISIONING_SECONDS=180
DISK_WARN_PERCENT=85

attention=0
report() {
    echo "ATTENTION  $*"
    attention=$((attention + 1))
}

# Containers labeled as this deployment's labs: id, name, state, lab label, age.
containers=$(docker ps -a --filter "label=linuxlab.deployment=$deployment" \
    --format '{{.ID}} {{.Names}} {{.State}} {{.Label "linuxlab.lab_id"}} {{.Label "linuxlab.managed"}} {{.RunningFor}}')
# Unfinished labs: key (the id as the runtime names it), status, seconds since
# creation and since the end.
labs=$(psql_on "$database" -F ' ' -c "
    SELECT replace(id::text, '-', ''), status,
           extract(epoch FROM now() - created_at)::int,
           coalesce(extract(epoch FROM now() - ended_at)::int, -1)
    FROM lab_sessions WHERE status <> 'terminated' ORDER BY created_at")

declare -A lab_status lab_age lab_ended container_state
while read -r key status age ended; do
    [ -n "$key" ] || continue
    lab_status[$key]=$status
    lab_age[$key]=$age
    lab_ended[$key]=$ended
done <<<"$labs"

container_count=0
while read -r id name state key managed running_for; do
    [ -n "$id" ] || continue
    container_count=$((container_count + 1))
    if [ "$managed" != true ] || [[ ! $key =~ ^[0-9a-f]{32}$ ]] || [ "$name" != "ll-lab-$key" ]; then
        report "unidentified container $name ($id): this deployment's label but not a lab the API created; left alone, inspect by hand"
        continue
    fi
    container_state[$key]=$state
    if [ -z "${lab_status[$key]:-}" ]; then
        report "orphan container $name ($state, created $running_for): no unfinished lab; the reaper removes it on its next pass"
    fi
done <<<"$containers"

unfinished=0
for key in "${!lab_status[@]}"; do
    unfinished=$((unfinished + 1))
    status=${lab_status[$key]}
    state=${container_state[$key]:-missing}
    case "$status" in
        ready)
            [ "$state" = running ] || report "lab $key is ready but its container is $state; the reaper ends it"
            ;;
        provisioning)
            [ "${lab_age[$key]}" -le "$STUCK_PROVISIONING_SECONDS" ] \
                || report "lab $key provisioning for ${lab_age[$key]} s"
            ;;
        terminating | failed)
            [ "${lab_ended[$key]}" -le "$STUCK_ENDING_SECONDS" ] \
                || report "lab $key $status for ${lab_ended[$key]} s (container $state): removal is not completing"
            ;;
    esac
done
echo "labs: $unfinished unfinished in the database, $container_count containers, capacity ${capacity:-?}"

api=$(compose ps --all -q api)
api_state=''
if [ -n "$api" ]; then
    # shellcheck disable=SC2086 # Every API container, if more than one exists.
    api_state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' $api | tr '\n' ' ')
fi
if [ "${api_state% }" != "running healthy" ]; then
    report "the API is not running and healthy: ${api_state:-no container}"
else
    errors=$(docker logs --since 1h "$api" 2>&1 \
        | grep -c -E 'lab reconciliation (failed|skipped)|lab removal failed|orphan removal failed|terminal cleanup failed|refusing to start' || true)
    [ "$errors" = 0 ] || report "$errors reconciliation or removal errors in the API's log in the last hour"
    orphans=$(docker logs --since 1h "$api" 2>&1 | grep -c 'removing orphan lab container' || true)
    [ "$orphans" = 0 ] || echo "note: the reaper removed $orphans orphan containers in the last hour"
fi

docker_root=$(docker info --format '{{.DockerRootDir}}')
for path in "$docker_root" "$(setting BACKUP_DIR)"; do
    if [ -z "$path" ] || [ ! -d "$path" ]; then
        continue
    fi
    used=$(df -P "$path" | awk 'NR == 2 {sub("%", "", $5); print $5}')
    [ "$used" -lt "$DISK_WARN_PERCENT" ] || report "disk of $path is $used% full"
done
available=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || true)
if [ -n "$available" ]; then
    # Each lab may take 512 MB.
    [ "$available" -ge 512 ] || report "only $available MB of memory available"
    echo "memory: $available MB available"
fi

if [ "$attention" -gt 0 ]; then
    echo "$attention items need attention"
    exit 1
fi
echo "ok"
