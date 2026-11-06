# shellcheck shell=bash disable=SC2034 # MINIO_* are read by the scripts that source this file.
# A MinIO server for one test run, sourced by infra/tests/remote-cases.sh,
# offsite-cases.sh, e2e.sh and disaster.sh: the real S3 API on 127.0.0.1 (or the
# address given), with a bucket "backups" and two credentials:
#   - the host's, allowed nothing but PutObject and GetObject under backups/host/
#     (docs/operations.md#copies-off-the-host);
#   - the recovery's, allowed nothing but ListBucket restricted to the prefix host/
#     and GetObject under backups/host/ (docs/operations.md#disaster-recovery).
# Every credential is made for the run. Needs minio and mc
# (infra/tests/install-minio.sh), curl and python3.

# free_port: a TCP port of 127.0.0.1 that nothing listens on now.
free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

minio_secret() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }

# minio_start DIR [ADDRESS]: starts MinIO on ADDRESS (default 127.0.0.1) with its data
# in DIR/data and its log in DIR/minio.log, and sets MINIO_PID, MINIO_ADDRESS,
# MINIO_PORT, MINIO_HOST_USER and MINIO_HOST_SECRET, MINIO_RECOVERY_USER and
# MINIO_RECOVERY_SECRET, and the mc alias "admin" (MC_CONFIG_DIR=DIR/mc), the
# administrator, for the test's own checks only.
minio_start() {
    local dir=$1 console_port
    MINIO_ADDRESS=${2:-127.0.0.1}
    mkdir -p "$dir"
    MINIO_PORT=$(free_port)
    console_port=$(free_port)
    MINIO_ROOT_USER="admin$(minio_secret | cut -c1-8)"
    MINIO_ROOT_PASSWORD=$(minio_secret)
    export MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_BROWSER=off MC_CONFIG_DIR="$dir/mc"
    minio server "$dir/data" --address "$MINIO_ADDRESS:$MINIO_PORT" \
        --console-address "127.0.0.1:$console_port" >"$dir/minio.log" 2>&1 &
    MINIO_PID=$!
    for _ in $(seq 150); do
        curl -fsS "http://$MINIO_ADDRESS:$MINIO_PORT/minio/health/ready" >/dev/null 2>&1 && break
        sleep 0.2
    done
    curl -fsS "http://$MINIO_ADDRESS:$MINIO_PORT/minio/health/ready" >/dev/null \
        || { echo "error: MinIO did not start (see $dir/minio.log)" >&2; return 1; }
    mc alias set admin "http://$MINIO_ADDRESS:$MINIO_PORT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc mb admin/backups >/dev/null
    printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":["arn:aws:s3:::backups/host/*"]}]}' \
        >"$dir/policy.json"
    MINIO_HOST_USER=backup-host-user
    MINIO_HOST_SECRET=$(minio_secret)
    mc admin policy create admin backup-host "$dir/policy.json" >/dev/null
    mc admin user add admin "$MINIO_HOST_USER" "$MINIO_HOST_SECRET" >/dev/null
    mc admin policy attach admin backup-host --user "$MINIO_HOST_USER" >/dev/null
    printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::backups"],"Condition":{"StringLike":{"s3:prefix":["host/","host/*"]}}},{"Effect":"Allow","Action":["s3:GetObject"],"Resource":["arn:aws:s3:::backups/host/*"]}]}' \
        >"$dir/recovery-policy.json"
    MINIO_RECOVERY_USER=backup-recovery-user
    MINIO_RECOVERY_SECRET=$(minio_secret)
    mc admin policy create admin backup-recovery "$dir/recovery-policy.json" >/dev/null
    mc admin user add admin "$MINIO_RECOVERY_USER" "$MINIO_RECOVERY_SECRET" >/dev/null
    mc admin policy attach admin backup-recovery --user "$MINIO_RECOVERY_USER" >/dev/null
}

# minio_rclone_config FILE PORT [ADDRESS]: writes rclone's configuration for the
# host's credential, remote "offsite", endpoint ADDRESS:PORT (MinIO, or a relay to
# it; default 127.0.0.1), readable by its owner only, as infra/ops/remote.sh requires.
minio_rclone_config() {
    (
        umask 077
        printf '%s\n' "[offsite]" "type = s3" "provider = Minio" \
            "access_key_id = $MINIO_HOST_USER" "secret_access_key = $MINIO_HOST_SECRET" \
            "endpoint = http://${3:-127.0.0.1}:$2" >"$1"
    )
}

# minio_recovery_config FILE PORT [ADDRESS]: the same for the recovery's credential,
# remote "recovery".
minio_recovery_config() {
    (
        umask 077
        printf '%s\n' "[recovery]" "type = s3" "provider = Minio" \
            "access_key_id = $MINIO_RECOVERY_USER" "secret_access_key = $MINIO_RECOVERY_SECRET" \
            "endpoint = http://${3:-127.0.0.1}:$2" >"$1"
    )
}
