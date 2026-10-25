#!/usr/bin/env bash
# Shows that infra/release/check-images.sh refuses contaminated images: it builds
# throwaway images on top of the given API and lab images, each with one defect
# (mission content copied in, a setuid file, a secret in the environment), and
# expects every one to be refused, and the clean images to pass.
#
#   infra/tests/check-images-detects.sh API_IMAGE LAB_IMAGE
set -euo pipefail

api=${1:?usage: check-images-detects.sh API_IMAGE LAB_IMAGE}
lab=${2:?usage: check-images-detects.sh API_IMAGE LAB_IMAGE}
check="$(dirname "$0")/../release/check-images.sh"
tag=linuxlab-check-images-test
failures=0

build() {
    local name=$1 dockerfile=$2
    docker build --quiet --tag "$tag:$name" - <<<"$dockerfile" >/dev/null
}

expect() {
    local outcome=$1 name=$2
    shift 2
    if "$check" "$@" >/dev/null 2>&1; then status=passed; else status=refused; fi
    if [ "$status" = "$outcome" ]; then
        echo "ok    $name: $status"
    else
        echo "FAIL  $name: $status, expected $outcome" >&2
        failures=$((failures + 1))
    fi
}

build content-in-app "FROM $api
USER root
RUN mkdir -p /app/content/missions/sample && echo 'slug: sample' > /app/content/missions/sample/mission.yaml
USER linuxlab"
build checkout-elsewhere "FROM $api
USER root
RUN mkdir -p /opt/linux-lab-content/content && touch /opt/linux-lab-content/content/README.md
USER linuxlab"
build solution-script "FROM $api
USER root
RUN mkdir -p /srv/tests && touch /srv/tests/solution-find.sh
USER linuxlab"
build secret-env "FROM $api
ENV SIGNUP_INVITE_CODE=synthetic-value"
build setuid "FROM $lab
USER root
RUN cp /bin/true /usr/local/bin/escalate && chmod u+s /usr/local/bin/escalate
USER student"

expect passed "clean images" --api "$api" --lab "$lab"
expect refused "mission content in /app/content" --api "$tag:content-in-app"
expect refused "private checkout elsewhere" --api "$tag:checkout-elsewhere"
expect refused "solution script" --api "$tag:solution-script"
expect refused "secret in the environment" --api "$tag:secret-env"
expect refused "setuid file in the lab image" --lab "$tag:setuid"

for name in content-in-app checkout-elsewhere solution-script secret-env setuid; do
    docker image rm --force "$tag:$name" >/dev/null
done
[ "$failures" -eq 0 ]
