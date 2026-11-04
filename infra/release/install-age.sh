#!/usr/bin/env bash
# Installs age and age-keygen, of the release the operation scripts are tested with
# (infra/ops/age.sh), into DIR: downloads the release's linux-amd64 archive from
# GitHub, refuses it unless its SHA-256 is the recorded one, and copies the two
# programs. CI installs into a directory of the job; the host into /usr/local/bin.
#
#   infra/release/install-age.sh [DIR]      (default /usr/local/bin)
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=../ops/age.sh
source "$(dirname "$0")/../ops/age.sh"

dir=${1:-/usr/local/bin}
[ "$(uname -m)" = x86_64 ] || { echo "error: only linux-amd64 is pinned here" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
archive="age-$AGE_VERSION-linux-amd64.tar.gz"
curl -fsSLo "$work/$archive" "https://github.com/FiloSottile/age/releases/download/$AGE_VERSION/$archive"
echo "$AGE_SHA256  $work/$archive" | sha256sum --check --strict --quiet \
    || { echo "error: $archive does not have the recorded SHA-256" >&2; exit 1; }
tar -xzf "$work/$archive" -C "$work" age/age age/age-keygen
mkdir -p "$dir"
install -m 0755 "$work/age/age" "$work/age/age-keygen" "$dir/"
"$dir/age" --version
