#!/usr/bin/env bash
# Rebuilds the deployment on a new host from its copies off the host alone: finds the
# newest backup confirmed there, downloads and checks it, decrypts it, rebuilds the
# release from the commits its manifest names, restores PostgreSQL and Caddy's data
# into new volumes, starts the stack and checks it. Nothing of the lost host is used.
# See docs/operations.md#disaster-recovery.
#
#   infra/ops/restore-offsite.sh --remote REMOTE --rclone-config FILE
#       --identity-file FILE [--identity-file FILE...]
#       --api-repo DIR --web-repo DIR [--receipt NAME] [--releases DIR]
#       [--recovery-dir DIR] [--check-only]
#
# --remote          rclone "remote:bucket/prefix" of the copies, with the recovery
#                   credential (list and read under the prefix, nothing else)
# --rclone-config   rclone's configuration file holding that credential: root's, 0600,
#                   never the host's own (BACKUP_RCLONE_CONFIG)
# --identity-file   a file holding an age private key the backups are encrypted to,
#                   on a tmpfs (docs/operations.md#disaster-recovery); repeatable
# --api-repo, --web-repo
#                   clones of linux-lab-api and linux-lab-web holding the manifest's
#                   commits; this script's own checkout must hold the API commit
# --receipt NAME    use this receipt (linuxlab-<...>.tar.age.receipt.json) instead of
#                   listing the prefix: for a credential that cannot list
# --releases DIR    where the release file is written (default /var/lib/linuxlab/releases)
# --recovery-dir DIR
#                   where the encrypted files are downloaded and the report written
#                   (default /var/lib/linuxlab/recovery)
# --check-only      stop once the package is downloaded, decrypted and checked: no
#                   Docker, no build, nothing restored
#
# Reads /etc/linuxlab/production.env (LINUXLAB_ENV_FILE), the new host's settings
# and secrets, written by the operator; never the old host's. Works on the Compose
# project linuxlab-prod (LINUXLAB_PROJECT), which must have no volume yet other than
# those an earlier run of this script made for the same backup: nothing that exists
# is ever overwritten, no database is dropped. Running it again after a failure goes
# on from where it stopped, checking again what was done. Exit status 0 only when
# the stack is up and every check passed.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

usage="usage: restore-offsite.sh --remote REMOTE --rclone-config FILE --identity-file FILE [--identity-file FILE...] --api-repo DIR --web-repo DIR [--receipt NAME] [--releases DIR] [--recovery-dir DIR] [--check-only]"
remote='' config='' api_repo='' web_repo='' receipt_name='' check_only=false
releases=/var/lib/linuxlab/releases recovery_root=/var/lib/linuxlab/recovery
identities=()
while [ $# -gt 0 ]; do
    case "$1" in
        --check-only) check_only=true; shift; continue ;;
        --remote | --rclone-config | --identity-file | --api-repo | --web-repo | --receipt | --releases | --recovery-dir)
            if [ $# -lt 2 ] || [ -z "$2" ]; then fail "$1 needs a value ($usage)"; fi
            ;;
        *) fail "unknown argument: $1 ($usage)" ;;
    esac
    case "$1" in
        --remote) remote=$2 ;;
        --rclone-config) config=$2 ;;
        --identity-file) identities+=(--identity "$2") ;;
        --api-repo) api_repo=$2 ;;
        --web-repo) web_repo=$2 ;;
        --receipt) receipt_name=$2 ;;
        --releases) releases=$2 ;;
        --recovery-dir) recovery_root=$2 ;;
    esac
    shift 2
done
if [ -z "$remote" ] || [ -z "$config" ]; then fail "--remote and --rclone-config are required ($usage)"; fi
[ "${#identities[@]}" -gt 0 ] || fail "no --identity-file: the backups cannot be decrypted without a key ($usage)"
if [ "$check_only" = false ]; then
    if [ -z "$api_repo" ] || [ -z "$web_repo" ]; then fail "--api-repo and --web-repo are required ($usage)"; fi
fi
if [ -n "$receipt_name" ]; then
    [[ $receipt_name =~ ^linuxlab-[0-9]{8}T[0-9]{6}Z-[0-9a-f]+-[a-z0-9-]+\.tar\.age\.receipt\.json$ ]] \
        || fail "--receipt must be the name of a receipt, linuxlab-<time>-<id>-<label>.tar.age.receipt.json"
fi
for dir in "$releases" "$recovery_root"; do
    [[ $dir == /* ]] || fail "$dir: an absolute path is required"
done

# Where the run is, for the report when it stops (EXIT trap).
STEP='checking the arguments'
step() {
    STEP=$1
    echo "== $STEP" >&2
}
STATE_DIR=''
STACK_STARTED=false
RUN_DIR=''
report_stop() {
    local status=$?
    if [ -n "$RUN_DIR" ]; then rm -rf -- "$RUN_DIR"; fi
    [ "$status" != 0 ] || return 0
    {
        echo
        echo "RECOVERY STOPPED while $STEP (exit status $status). Nothing that existed before was changed."
        if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
            echo "Done so far, recorded in $STATE_DIR:"
            find "$STATE_DIR" -maxdepth 1 \( -name 'done.*' -o -name 'restored.*' \) -printf '  %f\n' 2>/dev/null | sort
        fi
        if [ "$STACK_STARTED" = true ]; then
            echo "The stack was started and is left running for inspection: docker compose -p $PROJECT ... ps"
        fi
        echo "To go on: fix the cause and run the same command again; what was done is checked again, not redone."
        if [ "$check_only" = false ]; then
            echo "To start over (for example with another backup): docker compose -p $PROJECT down --volumes removes"
            echo "the volumes this recovery made (labeled linuxlab.recovery), which hold only restored data."
        fi
    } >&2
}
trap report_stop EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
step "checking the new host"
[ "$(uname -s)" = Linux ] || fail "Linux is required, this is $(uname -s)"
[ "$(uname -m)" = x86_64 ] || fail "x86_64 is required (the pinned age, rclone and gVisor), this is $(uname -m)"
if [ "$PROJECT" = "$PRODUCTION_PROJECT" ] && [ "$(id -u)" != 0 ]; then
    fail "run as root: the settings, keys and volumes are root's"
fi
for program in flock python3 sha256sum tar; do
    command -v "$program" >/dev/null || fail "$program is required"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "python3 3.10 or later is required"
check_age || exit 1
check_rclone || exit 1
[ -f "$ENV_FILE" ] || fail "the new host's environment file $ENV_FILE is missing: write it first (docs/operations.md#disaster-recovery)"
private_file "$ENV_FILE" "environment file" || exit 1
if [ "$check_only" = false ]; then
    command -v git >/dev/null || fail "git is required"
    command -v docker >/dev/null || fail "Docker Engine is required (docs/operations.md#preparing-the-host)"
    engine=$(docker version --format '{{.Server.Version}}' 2>/dev/null) || fail "the Docker Engine does not answer"
    [ "${engine%%.*}" -ge 28 ] 2>/dev/null || fail "Docker Engine 28 or later is required, this is $engine"
    docker compose version >/dev/null 2>&1 || fail "the Docker Compose plugin is required"
    docker info --format '{{range $name, $_ := .Runtimes}}{{$name}} {{end}}' | tr ' ' '\n' | grep -qx runsc \
        || fail "the runsc runtime (gVisor) is not registered with Docker: infra/release/install-gvisor.sh, then restart Docker"
    "$INFRA_DIR/release/check-env.sh" "$ENV_FILE" || fail "the environment file is not ready (see above)"
fi
for ((i = 1; i < ${#identities[@]}; i += 2)); do
    age_identity "${identities[i]}" || fail "refusing the identity file ${identities[i]}"
done

# The recovery credential: a remote and a private configuration file, never the
# host's own credential (which can write).
REMOTE=$remote
REMOTE_CONFIG=$config
check_remote_settings "$REMOTE" "$REMOTE_CONFIG" || exit 1
host_config=$(setting BACKUP_RCLONE_CONFIG)
if [ -n "$host_config" ] && [ -e "$host_config" ]; then
    if [ "$host_config" -ef "$REMOTE_CONFIG" ] || cmp -s -- "$host_config" "$REMOTE_CONFIG"; then
        fail "--rclone-config is the host's own credential (BACKUP_RCLONE_CONFIG): use the recovery credential, which can only list and read"
    fi
fi
destination=${REMOTE#*:}

take_lock
umask 077
mkdir -p -- "$recovery_root"
[ "$check_only" = true ] || mkdir -p -- "$releases"
chmod 700 -- "$recovery_root"
secure_directory() { check_directory "$1" "recovery directory" yes; }
secure_directory "$recovery_root" || exit 1

# ---------------------------------------------------------------------------
step "finding the backups confirmed off the host ($REMOTE)"
# Small objects only: a receipt or a checksum file is a few hundred bytes.
fetch_small() { rclone_run cat --no-traverse --count 65537 -- "$REMOTE/$1" 2>/dev/null >"$2" </dev/null; }
listing="$recovery_root/.receipts"
rm -rf -- "$listing"
mkdir -m 700 -- "$listing"
if [ -n "$receipt_name" ]; then
    names=$receipt_name
    note "using the receipt given, $receipt_name (no listing)"
else
    names=$(rclone_run lsf --files-only --max-depth 1 --include 'linuxlab-*.tar.age.receipt.json' -- "$REMOTE" 2>"$listing.err") \
        || fail "cannot list $REMOTE: $(head -c 300 "$listing.err" | tr '\n' ' ') (a credential without the right to list: give --receipt NAME)"
    rm -f -- "$listing.err"
fi
found=0
while IFS= read -r name; do
    [ -n "$name" ] || continue
    [[ $name =~ ^linuxlab-[0-9A-Za-z-]+\.tar\.age\.receipt\.json$ ]] || { note "ignored object $name"; continue; }
    found=$((found + 1))
    fetch_small "$name" "$listing/$name" || { note "$name: cannot be read; ignored"; rm -f -- "$listing/$name"; }
done <<<"$names"
[ "$found" -gt 0 ] || fail "no receipt under $REMOTE: no backup was confirmed there"
candidates=$(python3 -I "$OPS_DIR/receipts.py" select "$listing" --destination "$destination" 2>"$listing.refused") || true
if [ -s "$listing.refused" ]; then
    note "receipts refused:"
    sed 's/^/    /' "$listing.refused" >&2
fi
[ -n "$candidates" ] || fail "no valid receipt under $REMOTE ($found found, all refused)"
note "$(wc -l <<<"$candidates") valid receipt(s) of $found, newest first by verified_at"

# ---------------------------------------------------------------------------
step "downloading and checking the newest backup that reads back intact"
rejected=()
selected=''
while read -r verified_at object size sha256 _ backup_key; do
    dir="$recovery_root/$backup_key"
    [ -d "$dir" ] || mkdir -m 700 -- "$dir"
    cp -- "$listing/$object.receipt.json" "$dir/receipt.json"
    reason=''
    if ! fetch_small "$object.sha256" "$dir/$object.sha256"; then
        reason="its checksum object cannot be read"
    elif ! python3 -I "$OPS_DIR/receipts.py" checksum "$dir/$object.sha256" --receipt "$dir/receipt.json" 2>"$dir/checksum.err"; then
        reason="its checksum object does not match the receipt: $(head -c 200 "$dir/checksum.err")"
    else
        free=$(($(df -Pk -- "$dir" | awk 'NR == 2 {print $4}') * 1024))
        if [ "$free" -lt "$size" ]; then
            fail "not enough room in $dir for $object: $free bytes free, $size needed"
        fi
        note "$object: downloading $size bytes (verified off the host at $verified_at)"
        rm -f -- "$dir/$object.partial"
        if ! remote_fetch "$object" "$dir/$object.partial" </dev/null; then
            reason="the package cannot be read in full"
        elif [ "$(local_hash "$dir/$object.partial")" != "$sha256 $size" ]; then
            reason="the package read back is not the one the receipt describes (bytes or SHA-256)"
        else
            mv -f -- "$dir/$object.partial" "$dir/$object"
            if ! verify_checksum "$dir/$object" 2>"$dir/checksum.err"; then
                reason="the package does not match its checksum file"
            elif [ "$(head -c 21 -- "$dir/$object")" != "age-encryption.org/v1" ]; then
                reason="the package is not an age file"
            fi
        fi
    fi
    rm -f -- "$dir/checksum.err" "$dir/$object.partial"
    if [ -z "$reason" ]; then
        selected="$dir/$object"
        break
    fi
    rejected+=("$object: $reason")
    note "REJECTED $object: $reason; trying the next one"
    rm -f -- "$dir/$object"
done <<<"$candidates"
rm -rf -- "$listing" "$listing.refused"
if [ "${#rejected[@]}" -gt 0 ]; then
    note "WARNING: ${#rejected[@]} newer backup(s) confirmed off the host did not read back intact; an older one is used, and what was written after it is lost:"
    printf '    %s\n' "${rejected[@]}" >&2
fi
[ -n "$selected" ] || fail "no backup under $REMOTE reads back as its receipt says"
package=$selected
receipt="$dir/receipt.json"
STATE_DIR=$dir
note "selected $(basename -- "$package"): read back in full, bytes and SHA-256 as the receipt and its checksum say"

# ---------------------------------------------------------------------------
step "decrypting the package and checking it"
size=$(stat -c %s -- "$package")
# The decrypted package and, unpacked, its components: twice its size.
prepare_work $((size * 2 + 64 * 1024 * 1024))
age --decrypt "${identities[@]}" --output "$RUN_DIR/package.tar" "$package" \
    || fail "age could not decrypt $(basename -- "$package") (no matching key, or the file was changed)"
python3 -I "$OPS_DIR/package.py" check "$RUN_DIR/package.tar" --extract "$RUN_DIR/unpacked" >"$RUN_DIR/expected" \
    || fail "the decrypted file is not a valid backup package"
python3 -I "$OPS_DIR/recovery.py" manifest "$RUN_DIR/package.tar" >"$RUN_DIR/facts" \
    || fail "the package's manifest cannot be read"
rm -f -- "$RUN_DIR/package.tar"
fact() { sed -n "s/^$1 //p" "$RUN_DIR/facts"; }
expected() { sed -n "s/^$1 //p" "$RUN_DIR/expected"; }
[ "$(fact backup_id)-$(fact label)" = "$backup_key" ] \
    || fail "the package's manifest is of backup $(fact backup_id)-$(fact label), its receipt of $backup_key"
api_sha=$(fact api_sha)
web_sha=$(fact web_sha)
content_sha=$(fact content_sha)
dump="$RUN_DIR/unpacked/database/postgres.dump"
caddy_archive="$RUN_DIR/unpacked/caddy/caddy-data.tar.gz"
note "backup $backup_key, made $(fact created_at): API $api_sha, web $web_sha, content ${content_sha:-unknown}"
if [ -z "$content_sha" ]; then
    note "the content's commit is not recorded in this backup: $(fact content_source)"
fi

if [ "$check_only" = true ]; then
    echo "checked: $(basename -- "$package") ($backup_key) downloads, decrypts and checks; nothing was restored (--check-only)"
    exit 0
fi

# ---------------------------------------------------------------------------
step "rebuilding the release from the manifest's commits"
own_repo=$(cd "$INFRA_DIR/.." && pwd)
git -C "$own_repo" rev-parse --verify --quiet HEAD >/dev/null || fail "$own_repo is not a git checkout: run this script from a clone of linux-lab-api"
if ! git -C "$own_repo" cat-file -e "$api_sha^{commit}" 2>/dev/null \
    || ! git -C "$own_repo" merge-base --is-ancestor "$api_sha" HEAD; then
    fail "this script's checkout ($own_repo) does not hold the API commit $api_sha in its history: run it from a checkout at that commit or a later one"
fi
git -C "$api_repo" cat-file -e "$api_sha^{commit}" 2>/dev/null || fail "$api_sha is not a commit of $api_repo (fetch it from linux-lab-api)"
git -C "$web_repo" cat-file -e "$web_sha^{commit}" 2>/dev/null || fail "$web_sha is not a commit of $web_repo (fetch it from linux-lab-web)"
git -C "$api_repo" cat-file -e "$api_sha:infra/caddy/Dockerfile" 2>/dev/null || fail "$api_sha of $api_repo is not a commit of linux-lab-api"
git -C "$web_repo" cat-file -e "$web_sha:package-lock.json" 2>/dev/null || fail "$web_sha of $web_repo is not a commit of linux-lab-web"
# Room for the images and their build, and for the database once restored (a dump is
# compressed: several times its size), before anything is built.
docker_root=$(docker info --format '{{.DockerRootDir}}')
free=$(($(df -Pk -- "$docker_root" | awk 'NR == 2 {print $4}') * 1024))
needed=$((6 * 1024 * 1024 * 1024 + 10 * $(stat -c %s -- "$dump")))
[ "$free" -ge "$needed" ] \
    || fail "not enough room for Docker ($docker_root): $free bytes free, about $needed needed (6 GiB for the images, ten times the dump for the database)"
release="$releases/release-${api_sha:0:12}-${web_sha:0:12}.env"
if [ -f "$release" ] && (load_release "$release" && [ "$(release_value API_SHA)" = "$api_sha" ] && [ "$(release_value WEB_SHA)" = "$web_sha" ]) >/dev/null 2>&1; then
    note "release file $release and its images exist (an earlier run): checked again below"
else
    [ ! -e "$release" ] || mv -f -- "$release" "$release.stale.$(date -u +%Y%m%dT%H%M%SZ)"
    "$INFRA_DIR/release/build.sh" --api-repo "$api_repo" --api-sha "$api_sha" \
        --web-repo "$web_repo" --web-sha "$web_sha" --out "$releases" >&2 \
        || fail "the release could not be built"
fi
load_release "$release"
label_of() { docker image inspect --format "{{index .Config.Labels \"$2\"}}" "$1"; }
if [ "$(release_value API_SHA)" != "$api_sha" ] || [ "$(release_value WEB_SHA)" != "$web_sha" ]; then
    fail "$release names other commits than the manifest"
fi
[ "$(image_revision "$API_IMAGE")" = "$api_sha" ] || fail "the API image is not labeled as built from $api_sha"
[ "$(image_revision "$(release_value LAB_IMAGE)")" = "$api_sha" ] || fail "the lab image is not labeled as built from $api_sha"
[ "$(image_revision "$(release_value EDGE_IMAGE)")" = "$web_sha" ] || fail "the edge image is not labeled as built from $web_sha"
[ "$(label_of "$(release_value EDGE_IMAGE)" linuxlab.api_revision)" = "$api_sha" ] || fail "the edge image is not labeled with the API commit $api_sha"
for part in api web; do
    recorded=$(fact "${part}_label")
    sha=$api_sha
    [ "$part" = web ] && sha=$web_sha
    [ -z "$recorded" ] || [ "$recorded" = "$sha" ] || fail "the manifest's $part image label ($recorded) is not its commit"
done
note "images built from $api_sha and $web_sha, labels checked (their ids are new: $(release_value API_IMAGE_ID), the lost host's was $(fact api_image_id))"
touch "$STATE_DIR/done.release"

# ---------------------------------------------------------------------------
step "checking the project's volumes and PostgreSQL"
for volume in postgres-data caddy-data caddy-config; do
    name="${PROJECT}_$volume"
    if docker volume inspect "$name" >/dev/null 2>&1; then
        owner=$(docker volume inspect --format '{{index .Labels "linuxlab.recovery"}}' "$name")
        [ "$owner" = "$backup_key" ] \
            || fail "volume $name exists and was not made by a recovery of $backup_key (${owner:-no recovery label}): an existing deployment? Nothing was changed"
        note "volume $name: made by an earlier run of this recovery"
    else
        docker volume create --label "com.docker.compose.project=$PROJECT" --label "com.docker.compose.volume=$volume" \
            --label "linuxlab.recovery=$backup_key" "$name" >/dev/null
        note "volume $name: created"
    fi
done
compose up -d --wait postgres >/dev/null || fail "PostgreSQL did not start"
major=$(($(psql_on postgres -c "SHOW server_version_num") / 10000))
[ "$major" -ge "$(expected postgresql_major_at_least)" ] \
    || fail "the dump needs PostgreSQL $(expected postgresql_major_at_least) or later, the stack runs $major"
verify_dump "$dump" || fail "the package's dump cannot be read back in full"
database=$(database_name)
[[ $database =~ ^[a-z_][a-z0-9_]*$ ]] || fail "DATABASE_NAME is not a plain name: $database"
if [ "$(psql_on postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$database'")" = 0 ]; then
    psql_on postgres -c "CREATE DATABASE $database TEMPLATE template0"
fi
# Relations and extensions beyond what a new database has.
objects=$(psql_on "$database" -c "
    SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_toast')
         + (SELECT count(*) FROM pg_extension WHERE extname <> 'plpgsql')")

# ---------------------------------------------------------------------------
step "restoring PostgreSQL"
compare() {
    [ "$2" = "$3" ] || fail "the database differs from the manifest: $1 is ${2:--}, the manifest says ${3:--}"
}
check_database() {
    local revision content
    revision=$(alembic_revision "$database")
    compare "the Alembic revision" "${revision:--}" "$(expected alembic_revision)"
    compare "the schema fingerprint" "$(schema_fingerprint "$database")" "$(expected schema_fingerprint)"
    content=$(content_fingerprint "$database")
    compare "the missions" "${content:--}" "$(expected content)"
    compare "the roles" "$(database_roles "$database")" "linuxlab:true"
}
if [ "$objects" = 0 ]; then
    note "restoring the dump into $database (empty), in one transaction"
    compose exec -T postgres pg_restore -U linuxlab -d "$database" --exit-on-error --no-owner --single-transaction <"$dump" \
        || fail "pg_restore failed; its transaction was rolled back and $database is empty again"
    # Restored, not yet checked: a run stopped before the checks checks it next time.
    touch "$STATE_DIR/restored.database"
    psql_on "$database" -c "ANALYZE"
elif [ -e "$STATE_DIR/restored.database" ]; then
    note "$database was restored by an earlier run of this recovery: checked again, not restored again"
else
    fail "$database already holds $objects objects that this recovery did not restore: not overwritten"
fi
check_database
# Every sequence ahead of the values its column holds.
behind=$(psql_on "$database" -f - <<'SQL'
SELECT coalesce(string_agg(format('%s.%s', t.relname, a.attname), ' '), '')
FROM pg_depend AS d
JOIN pg_class AS s ON s.oid = d.objid AND s.relkind = 'S'
JOIN pg_class AS t ON t.oid = d.refobjid
JOIN pg_namespace AS n ON n.oid = t.relnamespace AND n.nspname = 'public'
JOIN pg_attribute AS a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
WHERE d.deptype IN ('a', 'i')
  AND (xpath('/row/b/text()', query_to_xml(format(
        'SELECT coalesce(max(%I), 0) > (SELECT CASE WHEN is_called THEN last_value ELSE last_value - 1 END FROM %I.%I) AS b FROM public.%I',
        a.attname, (SELECT nspname FROM pg_namespace WHERE oid = s.relnamespace), s.relname, t.relname),
      false, true, '')))[1]::text = 'true'
SQL
)
[ -z "$behind" ] || fail "sequences behind their columns after the restore: $behind"
touch "$STATE_DIR/done.database"
note "$database matches the manifest: Alembic revision, schema, missions, roles; sequences ahead of their columns"
restore_report "$database" | sed 's/^/    /' >&2

# ---------------------------------------------------------------------------
step "restoring Caddy's data"
in_volume() {
    local mode=$1
    shift
    docker run --rm -i --network none --user 0 -v "${PROJECT}_caddy-data:/data:$mode" --entrypoint "$1" "$API_IMAGE" "${@:2}"
}
if [ "$(fact caddy_included)" != yes ]; then
    note "the package holds no Caddy data ($(fact caddy_data)): Caddy will request new certificates"
elif [ -e "$STATE_DIR/done.caddy-data" ]; then
    note "Caddy's data was restored by an earlier run of this recovery"
else
    existing=$(in_volume ro find /data -mindepth 1 -maxdepth 1 -print)
    if [ -z "$existing" ]; then
        in_volume rw tar -xzf - -C /data <"$caddy_archive" || fail "Caddy's data could not be extracted into ${PROJECT}_caddy-data"
    fi
    # GNU tar compares every entry with the volume: content, mode, owner, time.
    in_volume ro tar --compare -zf - -C /data <"$caddy_archive" >"$RUN_DIR/caddy.diff" 2>&1 \
        || fail "${PROJECT}_caddy-data does not hold exactly the package's Caddy data: $(head -c 300 "$RUN_DIR/caddy.diff")"
    touch "$STATE_DIR/done.caddy-data"
    note "Caddy's data restored into ${PROJECT}_caddy-data, compared entry by entry with the package"
fi
site=$(setting SITE_ADDRESS)
restored_certificate=''
if [ "$(fact caddy_included)" = yes ]; then
    restored_certificate=$(python3 -I "$OPS_DIR/recovery.py" certificate "$caddy_archive" "$site") || restored_certificate=''
fi
config_volume=$(docker run --rm --network none --user 0 -v "${PROJECT}_caddy-config:/config:ro" --entrypoint find "$API_IMAGE" /config -mindepth 1 -maxdepth 1 -print)
[ -z "$config_volume" ] || [ -e "$STATE_DIR/done.started" ] \
    || fail "${PROJECT}_caddy-config is not empty before the first start: it must be new"

# ---------------------------------------------------------------------------
step "starting the stack"
compose up -d --wait || { STACK_STARTED=true; fail "the stack did not become healthy (docker compose -p $PROJECT ... ps, logs)"; }
STACK_STARTED=true
touch "$STATE_DIR/done.started"

# ---------------------------------------------------------------------------
step "checking the stack"
health=$(compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5).read().decode())") \
    || fail "the API's health check did not answer"
[ "$health" = '{"status":"ok","database":"ok","runtime":"ok"}' ] || fail "the API is not healthy: $health"
note "API: $health"
# The missions, read back by this release's own code: every mission's current version
# must load as provisioning and validation read it.
missions=$(compose run --rm --no-deps -T task python - <<'PY'
import asyncio, sys
from sqlalchemy import select
from linuxlab.config import get_settings
from linuxlab.content.models import Mission, MissionVersion
from linuxlab.content.stored import provisioning_spec, validation_spec
from linuxlab.db import create_engine

async def main():
    engine = create_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(
                select(Mission.slug, Mission.status, MissionVersion.spec).join(
                    MissionVersion,
                    (MissionVersion.mission_id == Mission.id) & (MissionVersion.version == Mission.current_version),
                )
            )).all()
    finally:
        await engine.dispose()
    broken = []
    for slug, status, spec in rows:
        try:
            provisioning_spec(spec)
            validation_spec(spec)
        except Exception:
            broken.append(slug)
    published = sum(1 for _, status, _ in rows if status == "published")
    print(len(rows), published, ",".join(broken) or "-")

asyncio.run(main())
PY
) || fail "the missions could not be read back with the release's code"
read -r mission_count published broken <<<"$missions"
[ "$broken" = - ] || fail "missions whose current version this release cannot use: $broken"
recorded_count=$(expected content | cut -d' ' -f1)
[ "$recorded_count" = - ] || [ "$mission_count" = "$recorded_count" ] \
    || fail "$mission_count missions read back, the manifest records $recorded_count"
note "missions: $mission_count, $published published, each current version loads with the release's code"
# The site's server in the configuration Caddy loaded from the release's Caddyfile
# listens on :443. Its HTTP to HTTPS redirect on :80 is a server Caddy adds by itself
# and does not show there: it is checked by a request to the published port instead.
listeners=$(compose exec -T caddy wget -q -O - http://127.0.0.1:2019/config/apps/http/servers \
    | python3 -I -c 'import json, sys; print(" ".join(sorted({a for s in json.load(sys.stdin).values() for a in s.get("listen", [])})))') \
    || fail "Caddy's loaded configuration cannot be read"
[[ " $listeners " == *" :443 "* ]] || fail "Caddy's loaded configuration has no server on :443 (listeners: ${listeners:-none})"
https_port=$(compose port caddy 443) || fail "Caddy publishes no port 443"
http_port=$(compose port caddy 80) || fail "Caddy publishes no port 80"
https_port=${https_port##*:} http_port=${http_port##*:}
note "Caddy: configuration from the release's Caddyfile, server on :443; ports 80 and 443 published ($http_port, $https_port)"
# Requests, not the ports alone: the redirect on 80, then the site and the API on 443.
python3 -I "$OPS_DIR/recovery.py" site 127.0.0.1 "$https_port" "$http_port" "$site" "${restored_certificate:--}" >"$STATE_DIR/site.log" 2>&1 \
    || { cat "$STATE_DIR/site.log" >&2; fail "the site does not answer as it should through Caddy"; }
sed 's/^/    /' "$STATE_DIR/site.log" >&2
touch "$STATE_DIR/done.checked"

# ---------------------------------------------------------------------------
STEP="writing the report"
{
    echo "recovered: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "backup: $backup_key ($(basename -- "$package")), made $(fact created_at), confirmed off the host $(sed -n 's/.*"verified_at": "\(.*\)".*/\1/p' "$receipt")"
    echo "rejected newer backups: ${#rejected[@]}"
    printf '  %s\n' "${rejected[@]}"
    echo "api: $api_sha"
    echo "web: $web_sha"
    echo "content: ${content_sha:-unknown (not recorded in this backup; the missions come from the database)}"
    echo "release file: $release"
    echo "database: $database, $(alembic_revision "$database"), missions $(expected content)"
    echo "caddy data: $(fact caddy_included); certificate for $site: ${restored_certificate:-none in the backup}"
} | sed '/^  $/d' >"$STATE_DIR/report.txt"
cat "$STATE_DIR/report.txt"
cat <<EOF

The platform is running from backup $backup_key. Before opening it:
  - point the DNS record of $site to this host, open 80 and 443 in the provider's firewall
    (docs/operations.md#disaster-recovery); Caddy uses the restored certificate until it
    renews it with ACME;
  - remove the identity files from this host, and the recovery credential;
  - set BACKUP_REMOTE and the host's own credential, and take a backup.
EOF
