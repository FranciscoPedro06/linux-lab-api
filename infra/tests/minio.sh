# shellcheck shell=bash disable=SC2034 # MINIO_* are read by the scripts that source this file.
# A MinIO server for one test run, sourced by infra/tests/remote-cases.sh and
# infra/tests/e2e.sh: the real S3 API on 127.0.0.1, with a bucket "backups" and the
# credential a host gets, allowed nothing but PutObject and GetObject under
# backups/host/ (docs/operations.md#copies-off-the-host). Every credential is made
# for the run. Needs minio and mc (infra/tests/install-minio.sh), curl and python3.

# free_port: a TCP port of 127.0.0.1 that nothing listens on now.
free_port() {
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

minio_secret() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }

# minio_start DIR: starts MinIO with its data in DIR/data and its log in DIR/minio.log,
# and sets MINIO_PID, MINIO_PORT, MINIO_HOST_USER, MINIO_HOST_SECRET and the mc alias
# "admin" (MC_CONFIG_DIR=DIR/mc), the administrator, for the test's own checks only.
minio_start() {
    local dir=$1 console_port
    mkdir -p "$dir"
    MINIO_PORT=$(free_port)
    console_port=$(free_port)
    MINIO_ROOT_USER="admin$(minio_secret | cut -c1-8)"
    MINIO_ROOT_PASSWORD=$(minio_secret)
    export MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_BROWSER=off MC_CONFIG_DIR="$dir/mc"
    minio server "$dir/data" --address "127.0.0.1:$MINIO_PORT" \
        --console-address "127.0.0.1:$console_port" >"$dir/minio.log" 2>&1 &
    MINIO_PID=$!
    for _ in $(seq 150); do
        curl -fsS "http://127.0.0.1:$MINIO_PORT/minio/health/ready" >/dev/null 2>&1 && break
        sleep 0.2
    done
    curl -fsS "http://127.0.0.1:$MINIO_PORT/minio/health/ready" >/dev/null \
        || { echo "error: MinIO did not start (see $dir/minio.log)" >&2; return 1; }
    mc alias set admin "http://127.0.0.1:$MINIO_PORT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
    mc mb admin/backups >/dev/null
    printf '%s' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:PutObject","s3:GetObject"],"Resource":["arn:aws:s3:::backups/host/*"]}]}' \
        >"$dir/policy.json"
    MINIO_HOST_USER=backup-host-user
    MINIO_HOST_SECRET=$(minio_secret)
    mc admin policy create admin backup-host "$dir/policy.json" >/dev/null
    mc admin user add admin "$MINIO_HOST_USER" "$MINIO_HOST_SECRET" >/dev/null
    mc admin policy attach admin backup-host --user "$MINIO_HOST_USER" >/dev/null
}

# minio_rclone_config FILE PORT: writes rclone's configuration for the host's
# credential, remote "offsite", endpoint 127.0.0.1:PORT (MinIO, or a relay to it),
# readable by its owner only, as infra/ops/remote.sh requires.
minio_rclone_config() {
    (
        umask 077
        printf '%s\n' "[offsite]" "type = s3" "provider = Minio" \
            "access_key_id = $MINIO_HOST_USER" "secret_access_key = $MINIO_HOST_SECRET" \
            "endpoint = http://127.0.0.1:$2" >"$1"
    )
}
