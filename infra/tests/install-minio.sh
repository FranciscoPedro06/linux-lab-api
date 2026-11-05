#!/usr/bin/env bash
# Installs MinIO's server and client (mc), for tests only: an S3-compatible store on
# 127.0.0.1 that infra/tests/remote-cases.sh and infra/tests/e2e.sh run as a process
# and remove afterwards. Downloads the pinned linux-amd64 releases from GitHub and
# refuses them unless their SHA-256 is the one recorded here. Never on a host.
#
#   infra/tests/install-minio.sh DIR
set -euo pipefail

MINIO_RELEASE=RELEASE.2025-09-07T16-13-09Z
MINIO_SHA256=7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f
MC_RELEASE=RELEASE.2025-08-13T08-35-41Z
MC_SHA256=01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891

dir=${1:?usage: install-minio.sh DIR}
[ "$(uname -m)" = x86_64 ] || { echo "error: only linux-amd64 is pinned here" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fetch() {
    local program=$1 release=$2 sum=$3 asset
    asset="$program.linux-amd64.$release"
    curl -fsSLo "$work/$program" "https://github.com/minio/$program/releases/download/$release/$asset"
    echo "$sum  $work/$program" | sha256sum --check --strict --quiet \
        || { echo "error: $asset does not have the recorded SHA-256" >&2; exit 1; }
    install -m 0755 "$work/$program" "$dir/$program"
}
mkdir -p "$dir"
fetch minio "$MINIO_RELEASE" "$MINIO_SHA256"
fetch mc "$MC_RELEASE" "$MC_SHA256"
"$dir/minio" --version | head -n 1
"$dir/mc" --version | head -n 1
