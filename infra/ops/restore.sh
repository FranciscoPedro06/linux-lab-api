#!/usr/bin/env bash
# Restores the database of an encrypted package from infra/ops/backup.sh into a NEW
# database on the deployment's PostgreSQL, never into an existing one, checks it
# against the package's manifest and reports what it holds. The running deployment
# keeps using its own database. See docs/operations.md#restoring.
#
#   infra/ops/restore.sh RELEASE_FILE PACKAGE.tar.age --identity-file FILE
#                        [--identity-file FILE...] [--keep]
#
# --identity-file names a file holding an age private key that is one of the
# package's recipients; repeat it to try several. Keys are read from those files only
# (never from the environment) by age itself, and never printed. They need not stay
# on the host: see docs/operations.md#restoring.
# Without --keep the new database is dropped afterwards: a restore test. With
# --keep it stays, named restore_<time>, for an emergency switch (DATABASE_NAME).
# Any other argument is refused before anything is done.
#
# Before any database is created, in this order: the package's checksum file must be
# next to it (PACKAGE.sha256) and match the encrypted bytes; the file must be an age
# file; age must decrypt all of it, which authenticates every byte, into a run
# directory of BACKUP_WORK_DIR (lib.sh), and only once age has exited with success is
# the result used; the package must check with infra/ops/package.py while it is
# unpacked there; the dump must read back in full; and the server must be of the
# dump's major version or later. After the restore, the Alembic revision, the schema
# and the missions must be those the manifest recorded. The run directory, with
# everything decrypted, is removed when the script ends, whatever the reason.
#
# Needs age (infra/release/install-age.sh) and python3 (3.10 or later) on the host.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

usage="usage: restore.sh RELEASE_FILE PACKAGE.tar.age --identity-file FILE [--identity-file FILE...] [--keep]"
if [ $# -lt 2 ]; then fail "$usage"; fi
release=$1
package=$2
shift 2
identities=()
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) RESTORE_KEEP=true; shift ;;
        --identity-file)
            [ $# -ge 2 ] || fail "--identity-file needs a file ($usage)"
            identities+=(--identity "$2")
            shift 2
            ;;
        *) fail "unknown argument: $1 ($usage)" ;;
    esac
done
[ "${#identities[@]}" -gt 0 ] || fail "no --identity-file: an encrypted package cannot be restored without a key ($usage)"
# Each key file, before age reads any: a regular file of this user (root), not a link,
# mode 0600 or 0400, on a path nobody else can change (infra/ops/age.sh).
for ((i = 1; i < ${#identities[@]}; i += 2)); do
    age_identity "${identities[i]}" || fail "refusing the identity file ${identities[i]}"
done
load_release "$release"
command -v python3 >/dev/null || fail "python3 is required (infra/ops/package.py)"
check_age || exit 1
take_lock
[ -f "$package" ] || fail "package not found: $package"
[[ $package == *.tar.age ]] || fail "refusing $package: only encrypted packages (.tar.age) are restored"
verify_checksum "$package" || fail "refusing $package: its checksum is missing or does not match"
[ "$(head -c 21 -- "$package")" = "age-encryption.org/v1" ] || fail "refusing $package: it is not an age file"

RUN_DIR=''
# shellcheck disable=SC2317 # Run by the EXIT trap.
cleanup() {
    drop_restore_target
    if [ -n "$RUN_DIR" ]; then rm -rf -- "$RUN_DIR"; fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# The decrypted package and, unpacked, its components: twice the encrypted size.
prepare_work $(($(stat -c %s -- "$package") * 2 + 64 * 1024 * 1024))
# age writes what it has decrypted before it finds a damaged chunk, then fails: the
# output is used only once age has exited with success, and removed in any case.
age --decrypt "${identities[@]}" --output "$RUN_DIR/package.tar" "$package" \
    || fail "refusing $package: age could not decrypt it (no matching key, or the file was changed or cut)"
python3 "$OPS_DIR/package.py" check "$RUN_DIR/package.tar" --extract "$RUN_DIR/unpacked" >"$RUN_DIR/expected" \
    || fail "refusing $package: it is not a valid backup package"
rm -f -- "$RUN_DIR/package.tar"
dump="$RUN_DIR/unpacked/database/postgres.dump"
verify_dump "$dump" || fail "refusing $package: its dump cannot be read back in full"

restore_database "$dump" "$RUN_DIR/expected"

if [ "$RESTORE_KEEP" = true ]; then
    echo "kept $RESTORE_TARGET. Labs recorded as unfinished in it may no longer exist, and labs"
    echo "created since the dump are not in it: see docs/operations.md#restoring."
else
    note "restore test passed; dropping $RESTORE_TARGET"
fi
