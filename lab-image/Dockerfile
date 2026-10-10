# syntax=docker/dockerfile:1

# Image for student labs. Runs as a read-only rootfs with tmpfs mounts provided by the runtime;
# see src/linuxlab/labs/runtime/spec.py for the container configuration.
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        coreutils \
        iproute2 \
        procps \
        python3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

# The base image ships an "ubuntu" user with uid 1000; the student takes that uid.
RUN userdel --remove ubuntu \
    && useradd --uid 1000 --user-group --create-home --shell /bin/bash student

# No setuid or setgid binaries: the student has no path to uid 0.
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} +

# Platform utilities that run as root through docker exec. Only root can open the
# directory, and the rootfs is read-only, so the student can neither read nor change them.
RUN install --directory --owner=root --group=root --mode=0700 /opt/labctl
COPY --chown=root:root --chmod=0700 labctl /opt/labctl/labctl

USER student
WORKDIR /home/student
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
