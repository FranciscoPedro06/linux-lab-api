#!/usr/bin/env bash
# Shows that infra/release/check-env.sh accepts a complete environment file and
# refuses incomplete or unsafe ones, for both entries, without printing values.
# Runs on Linux (it checks file modes); synthetic values only.
#
#   infra/tests/check-env-detects.sh
set -euo pipefail

check="$(cd "$(dirname "$0")/../release" && pwd)/check-env.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0
gid=999
if [ -S /var/run/docker.sock ]; then
    gid=$(stat -c %g /var/run/docker.sock)
fi

write() {
    local name=$1
    shift
    (umask 077; printf '%s\n' "$@" >"$work/$name")
}
# Two recipients of keys made for this run only, in its directory.
age-keygen -o "$work/operator.key" 2>/dev/null
age-keygen -o "$work/recovery.key" 2>/dev/null
operator=$(age-keygen -y "$work/operator.key")
recovery=$(age-keygen -y "$work/recovery.key")
write recipients.txt "# operator" "$operator" "" "# offline recovery" "$recovery"
good=(SITE_ADDRESS=lab.test.invalid PUBLIC_ORIGIN=https://lab.test.invalid
    POSTGRES_PASSWORD=0123456789abcdef0123456789abcdef SIGNUP_INVITE_CODE=synthetic-invite-0123456789
    DOCKER_SOCKET_GID="$gid" LAB_DEPLOYMENT=prod LAB_CAPACITY=4
    "BACKUP_AGE_RECIPIENTS_FILE=$work/recipients.txt")

expect() {
    local outcome=$1 name=$2
    shift 2
    if "$check" "$@" >"$work/out" 2>&1; then status=accepted; else status=refused; fi
    if [ "$status" = "$outcome" ]; then
        echo "ok    $name: $status"
    else
        echo "FAIL  $name: $status, expected $outcome" >&2
        cat "$work/out" >&2
        failures=$((failures + 1))
    fi
    if grep -q -e 0123456789abcdef -e synthetic-invite -e synthetic-token -e age1 -e AGE-SECRET-KEY "$work/out"; then
        echo "FAIL  $name: printed a value" >&2
        failures=$((failures + 1))
    fi
}

write good.env "${good[@]}"
expect accepted "complete file" "$work/good.env"
write example.env "${good[@]}" SITE_ADDRESS=lab.example.org PUBLIC_ORIGIN=https://lab.example.org
expect refused "example site left" "$work/example.env"
write origin.env "${good[@]}" PUBLIC_ORIGIN=http://lab.test.invalid
expect refused "origin not https://SITE_ADDRESS" "$work/origin.env"
write password.env "${good[@]}" POSTGRES_PASSWORD=short
expect refused "weak database password" "$work/password.env"
write invite.env "${good[@]}" SIGNUP_INVITE_CODE=short
expect refused "short invite code" "$work/invite.env"
write capacity.env "${good[@]}" LAB_CAPACITY=
expect refused "capacity missing" "$work/capacity.env"
write readable.env "${good[@]}"
chmod 644 "$work/readable.env"
expect refused "file readable by others" "$work/readable.env"

write tunnel.env "${good[@]}" NGROK_URL=https://lab.test.invalid
write ngrok.env NGROK_AUTHTOKEN=synthetic-token
expect accepted "ngrok entry, complete" "$work/tunnel.env" "$work/ngrok.env"
write empty-token.env NGROK_AUTHTOKEN=
expect refused "ngrok entry, no authtoken" "$work/tunnel.env" "$work/empty-token.env"
write mismatch.env "${good[@]}" NGROK_URL=https://other.test.invalid
expect refused "ngrok entry, NGROK_URL is not the site" "$work/mismatch.env" "$work/ngrok.env"
chmod 644 "$work/ngrok.env"
expect refused "ngrok entry, token file readable by others" "$work/tunnel.env" "$work/ngrok.env"

# Backup encryption: the recipients file and age itself.
recipients() {
    local name=$1
    shift
    write "$name" "$@"
    write "$name.env" "${good[@]}" "BACKUP_AGE_RECIPIENTS_FILE=$work/$name"
}
write no-recipients.env "${good[@]}" BACKUP_AGE_RECIPIENTS_FILE=
expect refused "no recipients file set" "$work/no-recipients.env"
write missing-recipients.env "${good[@]}" "BACKUP_AGE_RECIPIENTS_FILE=$work/nowhere.txt"
expect refused "recipients file missing" "$work/missing-recipients.env"
recipients one.txt "$operator"
expect refused "one recipient only" "$work/one.txt.env"
recipients twice.txt "$operator" "# again" "$operator"
expect refused "a recipient listed twice" "$work/twice.txt.env"
recipients invalid.txt "$operator" "$recovery" "age1notakey"
expect refused "a line that is not a recipient" "$work/invalid.txt.env"
recipients secret.txt "$operator" "$recovery" "$(grep AGE-SECRET-KEY "$work/operator.key")"
expect refused "a private key among the recipients" "$work/secret.txt.env"
recipients writable.txt "$operator" "$recovery"
chmod 664 "$work/writable.txt"
expect refused "recipients file writable by its group" "$work/writable.txt.env"
mkdir -m 770 "$work/shared"
(umask 077; printf '%s\n' "$operator" "$recovery" >"$work/shared/recipients.txt")
write shared.env "${good[@]}" "BACKUP_AGE_RECIPIENTS_FILE=$work/shared/recipients.txt"
expect refused "recipients file in a directory its group can change" "$work/shared.env"
ln -s "$work/recipients.txt" "$work/link.txt"
write link.env "${good[@]}" "BACKUP_AGE_RECIPIENTS_FILE=$work/link.txt"
expect refused "recipients file that is a link" "$work/link.env"
no_age=$(mktemp -d)
for program in dirname stat sed tail head grep sort uniq wc cat tr cut id readlink; do
    ln -s "$(command -v "$program")" "$no_age/$program"
done
bash=$(command -v bash)
if PATH=$no_age "$bash" "$check" "$work/good.env" >"$work/out" 2>&1; then status=accepted; else status=refused; fi
if [ "$status" = refused ] && grep -q "age is missing" "$work/out"; then
    echo "ok    age not installed: refused"
else
    echo "FAIL  age not installed: $status" >&2
    cat "$work/out" >&2
    failures=$((failures + 1))
fi
rm -rf "$no_age"

[ "$failures" -eq 0 ]
