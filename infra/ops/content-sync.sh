#!/usr/bin/env bash
# Syncs mission content from a checkout of the private repository into the
# deployment's database, with the release's API image. See
# docs/operations.md#syncing-mission-content.
#
#   infra/ops/content-sync.sh RELEASE_FILE CHECKOUT CONTENT_SHA
#
# CONTENT_SHA is the commit of linux-lab-content whose missions workflow passed
# against the release's API commit, under runc and runsc. The checkout must be at
# that commit with nothing modified or untracked. Its content/ directory is mounted
# read-only into a one-off task container, removed when the sync ends; no running
# service ever sees it, and it never enters an image.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

load_release "${1:-}"
checkout=${2:?usage: content-sync.sh RELEASE_FILE CHECKOUT CONTENT_SHA}
content_sha=${3:?usage: content-sync.sh RELEASE_FILE CHECKOUT CONTENT_SHA}
[[ $content_sha =~ ^[0-9a-f]{40}$ ]] || fail "CONTENT_SHA must be a full commit SHA"
[ -d "$checkout/content" ] || fail "$checkout has no content/ directory"

head=$(git -C "$checkout" rev-parse HEAD)
[ "$head" = "$content_sha" ] || fail "the checkout is at $head, not $content_sha"
dirty=$(git -C "$checkout" status --porcelain --untracked-files=all -- content)
[ -z "$dirty" ] || fail "content/ of the checkout has changes or untracked files"

note "syncing content $content_sha with API $API_SHA"
compose up -d --wait postgres >/dev/null
# The task runs as the checkout's owner (root on the host, see docs/operations.md),
# still without capabilities: the checkout stays readable by its owner only.
source_dir=$(cd "$checkout/content" && pwd)
compose run --rm --no-deps --user "$(stat -c %u:%g "$source_dir")" \
    -v "$(host_path "$source_dir"):/content:ro" \
    task linuxlab content sync --content-dir /content
note "content $content_sha synced"
