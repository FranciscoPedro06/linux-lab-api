from linuxlab.labs.runtime import LabContainerSpec
from linuxlab.labs.runtime.spec import build_container_config, container_name

SPEC = LabContainerSpec(lab_id="0b7c9d2e", image="linuxlab/lab-base:test")

# Snapshot of the isolation settings. A change here is a change to the threat model:
# update docs/threat-model.md in the same commit.
EXPECTED = {
    "Image": "linuxlab/lab-base:test",
    "User": "1000:1000",
    "WorkingDir": "/home/student",
    "Hostname": "linuxlab",
    "Env": ["LANG=C.UTF-8"],
    "Labels": {"linuxlab.managed": "true", "linuxlab.lab_id": "0b7c9d2e"},
    "HostConfig": {
        "Runtime": "runsc",
        "NetworkMode": "none",
        "Privileged": False,
        "ReadonlyRootfs": True,
        "Tmpfs": {
            "/home/student": "rw,nosuid,nodev,exec,size=64m,uid=1000,gid=1000,mode=0755",
            "/tmp": "rw,nosuid,nodev,exec,size=32m,mode=1777",
            "/run/lab": "rw,nosuid,nodev,noexec,size=1m,uid=1000,gid=1000,mode=0755",
        },
        "Binds": [],
        "Mounts": [],
        "Devices": [],
        "Memory": 536870912,
        "MemorySwap": 536870912,
        "NanoCpus": 500000000,
        "PidsLimit": 128,
        "CapDrop": ["ALL"],
        "CapAdd": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL"],
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


def test_container_config_matches_snapshot() -> None:
    assert build_container_config(SPEC, "runsc") == EXPECTED


def test_oci_runtime_is_passed_through() -> None:
    config = build_container_config(SPEC, "runc")

    assert config["HostConfig"]["Runtime"] == "runc"


def test_container_name_is_derived_from_lab_id() -> None:
    assert container_name("0b7c9d2e") == "ll-lab-0b7c9d2e"


def test_config_is_not_shared_between_calls() -> None:
    first = build_container_config(SPEC, "runsc")
    first["HostConfig"]["Tmpfs"]["/extra"] = "rw"
    first["HostConfig"]["CapAdd"].append("SYS_ADMIN")

    assert build_container_config(SPEC, "runsc") == EXPECTED
