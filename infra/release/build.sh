#!/usr/bin/env bash
# Builds the release images from exact commits of linux-lab-api and linux-lab-web,
# checks what ended up in them and writes the release file read by
# infra/compose.prod.yml. See docs/deployment.md#building-a-release.
#
#   infra/release/build.sh --api-repo DIR --api-sha SHA --web-repo DIR --web-sha SHA --out DIR
#
# Each tree is a `git archive` of its commit: untracked and ignored files of the
# checkouts (a .env, mission content copied by mistake) never reach a build context.
set -euo pipefail

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

fail() {
    echo "error: $*" >&2
    exit 1
}

api_repo='' api_sha='' web_repo='' web_sha='' out=''
while [ $# -gt 0 ]; do
    case "$1" in
        --api-repo) api_repo=$2; shift 2 ;;
        --api-sha) api_sha=$2; shift 2 ;;
        --web-repo) web_repo=$2; shift 2 ;;
        --web-sha) web_sha=$2; shift 2 ;;
        --out) out=$2; shift 2 ;;
        *) usage ;;
    esac
done
if [ -z "$api_repo" ] || [ -z "$api_sha" ] || [ -z "$web_repo" ] || [ -z "$web_sha" ] || [ -z "$out" ]; then
    usage
fi

for sha in "$api_sha" "$web_sha"; do
    [[ $sha =~ ^[0-9a-f]{40}$ ]] || fail "not a full commit SHA: $sha"
done
git -C "$api_repo" cat-file -e "$api_sha^{commit}" 2>/dev/null || fail "$api_sha is not a commit in $api_repo"
git -C "$web_repo" cat-file -e "$web_sha^{commit}" 2>/dev/null || fail "$web_sha is not a commit in $web_repo"
mkdir -p "$out"

work=$(mktemp -d "${out%/}/.build.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir "$work/api" "$work/web"
git -C "$api_repo" archive --format=tar "$api_sha" | tar -xf - -C "$work/api"
git -C "$web_repo" archive --format=tar "$web_sha" | tar -xf - -C "$work/web"

# The image copies content/, which must hold only the committed placeholder. Mission
# content is synced from the private checkout at deploy time, never built in.
extra=$(find "$work/api/content" -mindepth 1 ! -name .gitkeep -print)
[ -z "$extra" ] || fail "content/ of $api_sha is not empty: $extra"

api_image="linuxlab-api:$api_sha"
lab_image="linuxlab/lab-base:$api_sha"
edge_image="linuxlab-edge:$api_sha-$web_sha"
source_label=org.opencontainers.image.source
revision_label=org.opencontainers.image.revision

docker build --target prod --tag "$api_image" \
    --label "$source_label=https://github.com/FranciscoPedro06/linux-lab-api" \
    --label "$revision_label=$api_sha" "$work/api"
docker build --tag "$lab_image" \
    --label "$source_label=https://github.com/FranciscoPedro06/linux-lab-api" \
    --label "$revision_label=$api_sha" "$work/api/lab-image"
docker build --tag "$edge_image" --build-context "web=$work/web" \
    --label "$source_label=https://github.com/FranciscoPedro06/linux-lab-web" \
    --label "$revision_label=$web_sha" --label "linuxlab.api_revision=$api_sha" \
    --file "$work/api/infra/caddy/Dockerfile" "$work/api/infra/caddy"

"$(dirname "$0")/check-images.sh" --api "$api_image" --lab "$lab_image" --edge "$edge_image"

image_id() {
    docker image inspect --format '{{.Id}}' "$1"
}

release="${out%/}/release-${api_sha:0:12}-${web_sha:0:12}.env"
cat >"$release" <<EOF
# Release built $(date -u +%Y-%m-%dT%H:%M:%SZ) by infra/release/build.sh.
# Image ids are checked against the local images before every operation.
API_SHA=$api_sha
WEB_SHA=$web_sha
API_IMAGE=$api_image
API_IMAGE_ID=$(image_id "$api_image")
LAB_IMAGE=$lab_image
LAB_IMAGE_ID=$(image_id "$lab_image")
EDGE_IMAGE=$edge_image
EDGE_IMAGE_ID=$(image_id "$edge_image")
EOF
echo "release file: $release"
