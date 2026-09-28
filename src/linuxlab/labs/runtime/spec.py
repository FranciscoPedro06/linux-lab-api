"""Container configuration for labs.

This is the single place where lab isolation is defined. The values follow
docs/threat-model.md and are covered by a snapshot test; changing any of them
requires changing that test.
"""

from typing import Any

from linuxlab.labs.runtime.base import LabContainerSpec

LABEL_PREFIX = "linuxlab."
MANAGED_LABEL = f"{LABEL_PREFIX}managed"
LAB_ID_LABEL = f"{LABEL_PREFIX}lab_id"
CONTAINER_NAME_PREFIX = "ll-lab-"

STUDENT_UID = 1000
STUDENT_GID = 1000
STUDENT_HOME = "/home/student"

MEMORY_BYTES = 512 * 1024 * 1024
NANO_CPUS = 500_000_000  # 0.5 CPU
PIDS_LIMIT = 128

# Used only by platform processes that run as root through exec. The student
# runs as uid 1000 with no-new-privileges and has no effective capabilities.
PLATFORM_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL")

# Docker mounts tmpfs noexec unless told otherwise. Students run their own scripts
# from home and /tmp; /run/lab only holds platform data.
TMPFS = {
    STUDENT_HOME: f"rw,nosuid,nodev,exec,size=64m,uid={STUDENT_UID},gid={STUDENT_GID},mode=0755",
    "/tmp": "rw,nosuid,nodev,exec,size=32m,mode=1777",
    "/run/lab": f"rw,nosuid,nodev,noexec,size=1m,uid={STUDENT_UID},gid={STUDENT_GID},mode=0755",
}


def container_name(lab_id: str) -> str:
    return f"{CONTAINER_NAME_PREFIX}{lab_id}"


def build_container_config(spec: LabContainerSpec, oci_runtime: str) -> dict[str, Any]:
    """Docker Engine API body for POST /containers/create."""
    return {
        "Image": spec.image,
        "User": f"{STUDENT_UID}:{STUDENT_GID}",
        "WorkingDir": STUDENT_HOME,
        "Hostname": "linuxlab",
        "Env": ["LANG=C.UTF-8"],
        "Labels": {MANAGED_LABEL: "true", LAB_ID_LABEL: spec.lab_id},
        "NetworkDisabled": True,
        "HostConfig": {
            "Runtime": oci_runtime,
            "NetworkMode": "none",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "Tmpfs": dict(TMPFS),
            "Binds": [],
            "Mounts": [],
            "Devices": [],
            "Memory": MEMORY_BYTES,
            "MemorySwap": MEMORY_BYTES,
            "NanoCpus": NANO_CPUS,
            "PidsLimit": PIDS_LIMIT,
            "CapDrop": ["ALL"],
            "CapAdd": list(PLATFORM_CAPABILITIES),
            "SecurityOpt": ["no-new-privileges:true"],
            "IpcMode": "private",
            "Ulimits": [
                {"Name": "nofile", "Soft": 1024, "Hard": 1024},
                {"Name": "core", "Soft": 0, "Hard": 0},
            ],
            "Init": False,
            "AutoRemove": False,
            "RestartPolicy": {"Name": "no"},
        },
    }
