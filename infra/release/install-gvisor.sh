#!/usr/bin/env bash
# Installs gVisor (runsc, its containerd shim and helpers), of the release CI tests
# the labs with, into DIR, and registers runsc with the Docker Engine unless
# --no-register: downloads the release's x86_64 archive, refuses it unless its SHA-512
# is the recorded one, unpacks the programs and runs `runsc install`, which adds the
# runtime to /etc/docker/daemon.json (Docker must then be restarted). See
# docs/runtime.md.
#
#   infra/release/install-gvisor.sh [DIR] [--no-register]   (default /usr/local/bin)
set -euo pipefail

GVISOR_RELEASE=20260921.0
GVISOR_SHA512=7c899979bed334f0987888c41e545cede8e1f7e67a257978c8de244c3867e32bc30d08491e9af36720273c502acd01d8d86c9844ebcbe125790c43fe6bdf563e

dir=/usr/local/bin register=true
for argument in "$@"; do
    case "$argument" in
        --no-register) register=false ;;
        -*) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
        *) dir=$argument ;;
    esac
done
[ "$(uname -m)" = x86_64 ] || { echo "error: only x86_64 is pinned here" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSLo "$work/gvisor.tar.bz2" \
    "https://storage.googleapis.com/gvisor/releases/release/$GVISOR_RELEASE/x86_64/gvisor.tar.bz2"
echo "$GVISOR_SHA512  $work/gvisor.tar.bz2" | sha512sum --check --strict --quiet \
    || { echo "error: gvisor.tar.bz2 does not have the recorded SHA-512" >&2; exit 1; }
mkdir -p "$dir"
# As the release lays them out: the two programs and the gvisor-bin directory beside
# them. Needs bzip2.
tar -xjf "$work/gvisor.tar.bz2" -C "$dir" runsc containerd-shim-runsc-v1 gvisor-bin
"$dir/runsc" --version | head -n 1
if [ "$register" = true ]; then
    "$dir/runsc" install
fi
