# shellcheck shell=bash disable=SC2034 # AGE_VERSION and AGE_SHA256 are read by the scripts that source this file.
# age, which encrypts backup packages: the release the scripts are tested with, and
# the checks of a recipients file. Sourced by lib.sh, infra/release/check-env.sh and
# infra/release/install-age.sh; prints nothing secret.

# The release CI installs and tests, and the one infra/release/install-age.sh installs
# on the host. AGE_SHA256 is the SHA-256 of age-<version>-linux-amd64.tar.gz, as
# GitHub publishes it for the release's asset.
AGE_VERSION=v1.3.2
AGE_SHA256=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10

# An X25519 recipient: "age1" and 58 characters of the bech32 alphabet.
AGE_RECIPIENT='^age1[02-9ac-hj-np-z]{58}$'

# check_directory DIR WHAT STRICT: status 0 when only root or this user can change
# what DIR holds: owned by one of them, and not writable by its group or others. With
# STRICT=no, a directory of root's with the sticky bit (as /tmp) is accepted too: in it
# nobody can rename or remove an entry that is not theirs.
check_directory() {
    local dir=$1 what=$2 strict=$3 owner mode
    read -r owner mode < <(stat -L -c '%u %a' -- "$dir") || { echo "error: $what: cannot read $dir" >&2; return 1; }
    if [ "$owner" != 0 ] && [ "$owner" != "$(id -u)" ]; then
        echo "error: $what: the directory $dir is owned by uid $owner, neither root nor $(id -un)" >&2
        return 1
    fi
    if (((8#$mode & 8#022) != 0)); then
        if [ "$strict" = yes ] || [ "$owner" != 0 ] || (((8#$mode & 8#1000) == 0)); then
            echo "error: $what: the directory $dir has mode $mode: writable by its group or others" >&2
            return 1
        fi
    fi
}

# secure_file FILE WHAT: status 0 when FILE is a regular file, not a link, owned by
# this user (root in production), and nobody else can replace it: its directory and
# every directory above it, along the path as written and along the path once links
# are resolved, pass check_directory (the file's own directory with STRICT=yes). Says
# why otherwise; never reads the file.
secure_file() {
    local file=$1 what=$2 owner dir path
    [[ $file == /* ]] || file="$PWD/$file"
    if [ -L "$file" ] || [ ! -f "$file" ]; then
        echo "error: $what $file is not a regular file, or is a link" >&2
        return 1
    fi
    owner=$(stat -c %u -- "$file")
    if [ "$owner" != "$(id -u)" ]; then
        echo "error: $what $file is owned by uid $owner, not by $(id -un) (uid $(id -u))" >&2
        return 1
    fi
    for dir in "$(dirname -- "$file")" "$(readlink -f -- "$(dirname -- "$file")")"; do
        check_directory "$dir" "$what $file" yes || return 1
        path=$dir
        while [ "$path" != / ]; do
            path=$(dirname -- "$path")
            check_directory "$path" "$what $file" no || return 1
        done
    done
}

# age_identity FILE: status 0 when FILE can hold a private key for restore.sh: a
# secure_file, and no permission at all for its group or others. Never reads it.
age_identity() {
    local mode
    secure_file "$1" "identity file" || return 1
    mode=$(stat -c %a -- "$1")
    if (((8#$mode & 8#077) != 0)); then
        echo "error: identity file $1 has mode $mode: its group or others have access; 0600 or 0400" >&2
        return 1
    fi
    if [ ! -r "$1" ]; then
        echo "error: identity file $1 is not readable" >&2
        return 1
    fi
}

# Status 0 when `age` is on the PATH and is the tested release; says why otherwise.
check_age() {
    local version
    if ! command -v age >/dev/null; then
        echo "error: age is not installed (infra/release/install-age.sh)" >&2
        return 1
    fi
    version=$(age --version 2>/dev/null) || version=unknown
    if [ "$version" != "$AGE_VERSION" ]; then
        echo "error: age $version is installed; the scripts are tested with $AGE_VERSION (infra/release/install-age.sh)" >&2
        return 1
    fi
}

# Prints the recipients of FILE, one per line (comment lines, starting with "#", and
# blank lines left out), or says why FILE is refused (status 1): not a secure_file
# (another owner, a link, a directory on its path others can change), not readable,
# writable by its group or others (anyone who can add a line can read every later
# backup), a private key in it, a line that is not an X25519 recipient, a recipient
# listed twice, or fewer than two recipients. Never prints a line of it.
age_recipients() {
    local file=$1 line number=0 mode
    local -a found=()
    secure_file "$file" "recipients file" || return 1
    if [ ! -r "$file" ]; then
        echo "error: recipients file $file is not readable" >&2
        return 1
    fi
    mode=$(stat -c %a "$file")
    if (((8#$mode & 8#022) != 0)); then
        echo "error: recipients file $file has mode $mode: writable by its group or others" >&2
        return 1
    fi
    if grep -q 'AGE-SECRET-KEY-' "$file"; then
        echo "error: recipients file $file holds a private key: it must hold public keys only" >&2
        return 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        number=$((number + 1))
        line=${line%$'\r'}
        line=${line#"${line%%[![:space:]]*}"}
        line=${line%"${line##*[![:space:]]}"}
        [ -z "$line" ] || [ "${line:0:1}" = "#" ] && continue
        if ! [[ $line =~ $AGE_RECIPIENT ]]; then
            echo "error: recipients file $file, line $number: not an age X25519 recipient" >&2
            return 1
        fi
        found+=("$line")
    done <"$file"
    if [ "$(printf '%s\n' "${found[@]}" | sort | uniq -d | wc -l)" != 0 ]; then
        echo "error: recipients file $file lists a recipient twice" >&2
        return 1
    fi
    if [ "${#found[@]}" -lt 2 ]; then
        echo "error: recipients file $file has ${#found[@]} recipient(s); at least two are required (the operator's and an offline recovery key)" >&2
        return 1
    fi
    printf '%s\n' "${found[@]}"
}
