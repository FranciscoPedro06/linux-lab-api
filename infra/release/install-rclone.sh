#!/usr/bin/env bash
# Installs rclone, of the release the operation scripts are tested with
# (infra/ops/remote.sh), into DIR: downloads the release's linux-amd64 archive from
# GitHub, refuses it unless its SHA-256 is the recorded one, and copies the program.
# CI installs into a directory of the job; the host into /usr/local/bin. Needs curl,
# sha256sum and python3 (to unpack the zip archive).
#
#   infra/release/install-rclone.sh [DIR]      (default /usr/local/bin)
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=../ops/remote.sh
source "$(dirname "$0")/../ops/remote.sh"

dir=${1:-/usr/local/bin}
[ "$(uname -m)" = x86_64 ] || { echo "error: only linux-amd64 is pinned here" >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
name="rclone-$RCLONE_VERSION-linux-amd64"
curl -fsSLo "$work/$name.zip" "https://github.com/rclone/rclone/releases/download/$RCLONE_VERSION/$name.zip"
echo "$RCLONE_SHA256  $work/$name.zip" | sha256sum --check --strict --quiet \
    || { echo "error: $name.zip does not have the recorded SHA-256" >&2; exit 1; }
# Only the one member, by its exact name, to a file of this script's choosing.
python3 -I - "$work/$name.zip" "$name/rclone" "$work/rclone" <<'PY'
import shutil, sys, zipfile
archive, member, output = sys.argv[1:4]
with zipfile.ZipFile(archive) as source, source.open(member) as data, open(output, "xb") as out:
    shutil.copyfileobj(data, out)
PY
mkdir -p "$dir"
install -m 0755 "$work/rclone" "$dir/rclone"
"$dir/rclone" version | head -n 1
