#!/usr/bin/env bash
# Real filesystem cases of infra/ops/age.sh and lib.sh: who owns the recipients file
# and the identity files, and the directories above them; which filesystem the work
# directory is on, and its room. Nothing is a stand-in: files, owners, modes, links
# and mounts are real. Keys are made for the run and removed with it.
#
#   infra/tests/key-files-cases.sh
#
# Run as an unprivileged user and as root (CI runs both, and once in a container,
# whose files are on overlayfs). Cases that need root, or a filesystem given in the
# environment, print "skip" without it:
#   KEY_CASES_RAMFS=DIR         a ramfs mount this user can create a directory in
#   KEY_CASES_SMALL_TMPFS=DIR   a tmpfs mount of 1 MiB this user can create a directory in
#   KEY_CASES_DISK_DIRS="DIR..."  directories on disk filesystems (ext4...) to refuse
#   KEY_CASES_REQUIRE_ALL=1     fail instead of skipping (CI)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
ops="$here/../ops"
work=$(mktemp -d)
[ "$(stat -f -c %T /dev/shm)" = tmpfs ] || { echo "error: /dev/shm is not a tmpfs here" >&2; exit 1; }
shm=$(mktemp -d /dev/shm/key-cases.XXXXXX)
cleanup() {
    chmod -R u+rwx "$work" 2>/dev/null || true
    rm -rf "$work" "$shm"
    for dir in "${KEY_CASES_RAMFS:-}" "${KEY_CASES_SMALL_TMPFS:-}"; do
        if [ -n "$dir" ]; then rm -rf "$dir"/key-cases.*; fi
    done
}
trap cleanup EXIT
export LINUXLAB_ENV_FILE="$work/env" LINUXLAB_PROJECT=linuxlab-key-cases
export LINUXLAB_TEST_LOCK_FILE="$work/ops.lock"
# Swap is checked below with files of its own; until then, none.
printf 'Filename\tType\tSize\tUsed\tPriority\n' >"$work/swaps"
export LINUXLAB_TEST_SWAPS="$work/swaps"
: >"$LINUXLAB_ENV_FILE"
# shellcheck source-path=SCRIPTDIR source=../ops/lib.sh
source "$ops/lib.sh"
set +e
me=$(id -u)
root=false
[ "$me" = 0 ] && root=true
failures=0
skips=0

result() {
    if [ "$1" = 0 ]; then echo "ok    $2"; else echo "FAIL  $2" >&2; sed 's/^/      /' "$work/err" >&2; failures=$((failures + 1)); fi
}
# accepted NAME COMMAND...: COMMAND succeeds.
accepted() {
    local name=$1
    shift
    if ("$@") >/dev/null 2>"$work/err"; then result 0 "$name: accepted"; else result 1 "$name: accepted"; fi
}
# refused NAME REASON COMMAND...: COMMAND fails and says REASON.
refused() {
    local name=$1 reason=$2
    shift 2
    if ("$@") >/dev/null 2>"$work/err"; then
        result 1 "$name: refused"
    elif grep -q -- "$reason" "$work/err"; then
        result 0 "$name: refused ($reason)"
    else
        echo "      expected the reason: $reason" >"$work/err.reason"
        cat "$work/err.reason" >>"$work/err"
        result 1 "$name: refused, for the right reason"
    fi
}
skip() {
    if [ "${KEY_CASES_REQUIRE_ALL:-}" = 1 ]; then
        echo "FAIL  $1: not run ($2)" >&2
        failures=$((failures + 1))
    else
        echo "skip  $1 ($2)"
        skips=$((skips + 1))
    fi
}
other_uid() { echo 1000; [ "$me" != 1000 ] || echo 1001; }

# Two public keys of keys made for the run, in a file of mode 0644 in DIR.
age-keygen -o "$work/one.key" 2>/dev/null
age-keygen -o "$work/two.key" 2>/dev/null
recipients_in() {
    mkdir -p "$1"
    { age-keygen -y "$work/one.key"; age-keygen -y "$work/two.key"; } >"$1/recipients.txt"
    chmod 644 "$1/recipients.txt"
    echo "$1/recipients.txt"
}
identity_in() {
    mkdir -p "$1"
    cp "$work/one.key" "$1/${2:-operator}.key"
    chmod 600 "$1/${2:-operator}.key"
    echo "$1/${2:-operator}.key"
}
safe() { mkdir -p "$1" && chmod 700 "$1" && echo "$1"; }

echo "== as uid $me ($(id -un)); this tree on $(stat -f -c %T "$work"), under $(stat -c '%U %a' /tmp) /tmp"

echo "== recipients file"
file=$(recipients_in "$(safe "$work/etc/linuxlab")")
chmod 755 "$work/etc"
if [ "$root" = true ]; then
    accepted "root's file, 0644, in root's 0700 directory under 0755 ones (production)" age_recipients "$file"
else
    accepted "this user's file, 0644, in its 0700 directory under a sticky /tmp (tests)" age_recipients "$file"
fi
if [ "$root" = true ]; then
    chown "$(other_uid)" "$file"
    refused "a file owned by another user" "is owned by uid" age_recipients "$file"
    chown 0 "$file"
else
    refused "a file owned by another user (root's /etc/passwd)" "is owned by uid 0" age_recipients /etc/passwd
fi
chmod 770 "$work/etc/linuxlab"
refused "its directory writable by its group" "has mode 770" age_recipients "$file"
chmod 707 "$work/etc/linuxlab"
refused "its directory writable by others" "has mode 707" age_recipients "$file"
chmod 700 "$work/etc/linuxlab"
chmod 777 "$work/etc"
refused "a directory above it writable by others (not sticky)" "has mode 777" age_recipients "$file"
chmod 1777 "$work/etc"
if [ "$root" = true ]; then
    accepted "a directory above it of root's, sticky (as /tmp)" age_recipients "$file"
else
    refused "a directory above it sticky but not root's" "has mode 1777" age_recipients "$file"
fi
chmod 755 "$work/etc"
if [ "$root" = true ]; then
    chown "$(other_uid)" "$work/etc"
    refused "a directory above it owned by another user" "owned by uid" age_recipients "$file"
    chown 0 "$work/etc"
fi
ln -s "$file" "$work/etc/linuxlab/link.txt"
refused "a link to a valid file" "is a link" age_recipients "$work/etc/linuxlab/link.txt"
mkdir -m 777 "$work/open"
mkdir -m 700 "$work/open/inner"
cp -p "$file" "$work/open/inner/recipients.txt"
ln -s "$work/open/inner" "$work/etc/linuxlab/via"
refused "a path through a linked directory whose real path others can change" "has mode 777" \
    age_recipients "$work/etc/linuxlab/via/recipients.txt"
accepted "a relative path to a valid file" bash -c "cd '$work/etc' && source '$ops/age.sh' && age_recipients linuxlab/recipients.txt"

echo "== identity files"
keys=$(safe "$work/run/linuxlab-key")
operator=$(identity_in "$keys" operator)
recovery=$(identity_in "$keys" recovery)
accepted "a key of this user, 0600, in a 0700 directory" age_identity "$operator"
accepted "a second key, the same way" age_identity "$recovery"
chmod 400 "$operator"
accepted "a key of mode 0400" age_identity "$operator"
chmod 644 "$operator"
refused "a key of mode 0644" "has mode 644" age_identity "$operator"
chmod 640 "$operator"
refused "a key of mode 0640" "has mode 640" age_identity "$operator"
chmod 604 "$operator"
refused "a key of mode 0604" "has mode 604" age_identity "$operator"
chmod 600 "$operator"
ln -s "$operator" "$keys/link.key"
refused "a link to a key" "is a link" age_identity "$keys/link.key"
if [ "$root" = true ]; then
    chown "$(other_uid)" "$operator"
    refused "a key owned by another user" "is owned by uid" age_identity "$operator"
    chown 0 "$operator"
else
    refused "a file owned by another user (root's /etc/shadow or /etc/passwd)" "is owned by uid 0" age_identity /etc/passwd
fi
chmod 770 "$keys"
refused "a key in a directory its group can change" "has mode 770" age_identity "$operator"
chmod 700 "$keys"
refused "a key file that does not exist" "not a regular file" age_identity "$keys/missing.key"
age --encrypt -r "$(age-keygen -y "$work/one.key")" -r "$(age-keygen -y "$work/two.key")" \
    -o "$work/data.age" <<<"for two recipients"
age_recipients_decrypt() { age --decrypt --identity "$1" "$work/data.age"; }
accepted "the first key alone decrypts what was encrypted to both" age_recipients_decrypt "$operator"
cp "$work/two.key" "$recovery"
accepted "the second key alone decrypts it too" age_recipients_decrypt "$recovery"

echo "== the work directory"
prepare() { printf 'BACKUP_WORK_DIR=%s\n' "$1" >"$LINUXLAB_ENV_FILE"; prepare_work "${2:-1}"; }
default_existed=false
[ -e /run/linuxlab-backup ] && default_existed=true
accepted "on tmpfs ($(stat -f -c %T "$shm"))" prepare "$shm/work"
test_mode() { [ "$(stat -c %a "$shm/work")" = 700 ] && [ "$(stat -c %u "$shm/work")" = "$me" ]; }
accepted "  made 0700, this user's" test_mode
if [ -n "${KEY_CASES_RAMFS:-}" ]; then
    [ "$(stat -f -c %T "$KEY_CASES_RAMFS")" = ramfs ] || { echo "error: $KEY_CASES_RAMFS is not a ramfs" >&2; exit 1; }
    accepted "on ramfs" prepare "$KEY_CASES_RAMFS/key-cases.work"
else
    skip "on ramfs" "KEY_CASES_RAMFS not given"
fi
for dir in "$work" ${KEY_CASES_DISK_DIRS:-}; do
    fs=$(stat -f -c %T "$dir")
    refused "on $fs ($dir), a disk" "is on $fs, not in memory" prepare "$dir/key-cases.work"
done
no_run_dirs() { [ -z "$(find "$work" -name 'run.*' 2>/dev/null)" ]; }
accepted "  and no run directory was made on any disk" no_run_dirs
if [ "$default_existed" = false ]; then
    default_untouched() { [ ! -e /run/linuxlab-backup ]; }
    accepted "  nor anything made at the default path instead (no fallback)" default_untouched
fi
mkdir -m 700 "$shm/real"
ln -s "$shm/real" "$shm/linked"
refused "a link to a tmpfs directory" "is not a directory" prepare "$shm/linked"
refused "not enough room on a tmpfs (/dev/shm, about 1 PB needed)" "not enough room" prepare "$shm/work" 1000000000000000
if [ -n "${KEY_CASES_SMALL_TMPFS:-}" ]; then
    refused "not enough room on a 1 MiB tmpfs (64 MiB needed)" "not enough room" \
        prepare "$KEY_CASES_SMALL_TMPFS/key-cases.work" $((64 * 1024 * 1024))
else
    skip "not enough room on a 1 MiB tmpfs" "KEY_CASES_SMALL_TMPFS not given"
fi

echo "== swap, while the work directory is a tmpfs"
# swaps LINE...: a /proc/swaps of the header and these lines, read by check_swap.
swaps() {
    printf 'Filename\tType\tSize\tUsed\tPriority\n' >"$work/swaps.case"
    if [ $# -gt 0 ]; then printf '%s\n' "$@" >>"$work/swaps.case"; fi
    SWAPS="$work/swaps.case"
}
swaps
accepted "no swap" prepare "$shm/work"
swaps "/swapfile                               file\t\t4194300\t0\t-2"
refused "a swap file on disk" "swap in use and not encrypted: /swapfile" prepare "$shm/work"
swaps "/dev/sda3                               partition\t1048572\t0\t-2"
refused "a swap partition" "swap in use and not encrypted: /dev/sda3" prepare "$shm/work"
swaps "/mnt/my\\040swap                          file\t\t1024\t0\t-2"
refused "a swap file whose name has a space" "not encrypted: /mnt/my swap" prepare "$shm/work"
swaps "/dev/zram0                              partition\t1048572\t0\t100"
accepted "zram (compressed memory)" prepare "$shm/work"
swaps "/dev/zram0                              partition\t1048572\t0\t100" \
    "/swapfile                               file\t\t4194300\t0\t-2"
refused "zram and a swap file" "not encrypted: /swapfile" prepare "$shm/work"
no_new_run_dirs() { [ "$(find "$shm/work" -mindepth 1 -maxdepth 1 -name 'run.*' | wc -l)" -le 1 ]; }
accepted "  no run directory made when refused" no_new_run_dirs
SWAPS="$work/swaps.missing"
refused "swaps unreadable" "cannot read" prepare "$shm/work"
if [ -n "${KEY_CASES_RAMFS:-}" ]; then
    swaps "/swapfile                               file\t\t4194300\t0\t-2"
    accepted "on ramfs, whose pages are never swapped, swap does not matter" \
        prepare "$KEY_CASES_RAMFS/key-cases.work"
fi
if [ -r /proc/swaps ]; then
    SWAPS=/proc/swaps
    if [ "$(sed 1d /proc/swaps | wc -l)" = 0 ]; then
        accepted "this host: no swap in /proc/swaps" prepare "$shm/work"
    else
        echo "note  this host has swap: $(sed 1d /proc/swaps | awk '{print $1}' | paste -sd ' ')"
    fi
fi

[ "$failures" -eq 0 ] || { echo "$failures key file cases failed" >&2; exit 1; }
echo "all key file cases passed ($skips skipped)"
