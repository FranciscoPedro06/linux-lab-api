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
# service ever sees it, and it never enters an image. The sync records CONTENT_SHA in
# the database (content_syncs), which backups then name as the content's commit.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

load_release "${1:-}"
# Under the operations' lock: a backup records the missions it dumps (lib.sh).
take_lock
checkout=${2:?usage: content-sync.sh RELEASE_FILE CHECKOUT CONTENT_SHA}
content_sha=${3:?usage: content-sync.sh RELEASE_FILE CHECKOUT CONTENT_SHA}
[[ $content_sha =~ ^[0-9a-f]{40}$ ]] || fail "CONTENT_SHA must be a full commit SHA"
[ -d "$checkout/content" ] || fail "$checkout has no content/ directory"

head=$(git -C "$checkout" rev-parse HEAD)
[ "$head" = "$content_sha" ] || fail "the checkout is at $head, not $content_sha"
# Ignored files too: anything in content/ that is not the commit's would be synced
# under its name.
dirty=$(git -C "$checkout" status --porcelain --untracked-files=all --ignored -- content)
[ -z "$dirty" ] || fail "content/ of the checkout has changes, untracked or ignored files"

note "syncing content $content_sha with API $API_SHA"
compose up -d --wait postgres >/dev/null
# The task runs as the checkout's owner (root on the host, see docs/operations.md),
# still without capabilities: the checkout stays readable by its owner only.
source_dir=$(cd "$checkout/content" && pwd)
compose run --rm --no-deps --user "$(stat -c %u:%g "$source_dir")" \
    -v "$(host_path "$source_dir"):/content:ro" \
    task linuxlab content sync --content-dir /content --source-commit "$content_sha"
note "content $content_sha synced"
