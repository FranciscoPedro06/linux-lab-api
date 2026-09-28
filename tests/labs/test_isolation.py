"""Isolation checks run inside a real lab container.

These assert observed behavior, not configuration: each test runs commands in the
lab and checks what the kernel (or gVisor) actually allows.
"""

import time

import aiodocker
import pytest

from linuxlab.labs.runtime import ContainerInfo, ExecUser
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.runtime.spec import MEMORY_BYTES, NANO_CPUS, PIDS_LIMIT

from .support import OCI_RUNTIME, docker_inspect, running_lab, sh

pytestmark = pytest.mark.docker

# CAP_CHOWN (0) | CAP_DAC_OVERRIDE (1) | CAP_FOWNER (3) | CAP_KILL (5)
PLATFORM_CAPABILITY_MASK = 0x2B


def _status_field(status: bytes, name: str) -> str:
    for line in status.decode().splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    raise AssertionError(f"{name} not found in /proc/self/status")


# Identity


async def test_student_is_not_root(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, "id -u; id -g; id -un")

    assert result.stdout == b"1000\n1000\nstudent\n"


async def test_pid1_is_tini_running_as_student(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, "cat /proc/1/comm; awk '/^Uid:/ {print $2}' /proc/1/status")

    assert result.stdout == b"tini\n1000\n"


async def test_student_has_no_capabilities(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    status = (await sh(runtime, lab, "cat /proc/self/status")).stdout

    for field in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        assert int(_status_field(status, field), 16) == 0, field
    assert _status_field(status, "NoNewPrivs") == "1"


async def test_platform_root_has_only_the_four_capabilities(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    status = (await sh(runtime, lab, "cat /proc/self/status", user="root")).stdout

    assert int(_status_field(status, "CapEff"), 16) == PLATFORM_CAPABILITY_MASK
    assert int(_status_field(status, "CapBnd"), 16) == PLATFORM_CAPABILITY_MASK
    assert _status_field(status, "NoNewPrivs") == "1"


async def test_no_setuid_binaries_or_sudo(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    setuid = await sh(runtime, lab, "find / -xdev -type f -perm /6000 2>/dev/null", user="root")
    sudo = await sh(runtime, lab, "command -v sudo")

    assert setuid.stdout == b""
    assert sudo.exit_code != 0


async def test_student_cannot_become_root(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, "su -c id root </dev/null")

    assert result.exit_code != 0


# Capabilities used by platform processes. Each case performs the operation the platform
# will need, first with every capability and then with the one under test removed from
# the effective set, to show that each capability is both sufficient and necessary.

CAPABILITY_PROBE = r"""
import ctypes, os, sys

BITS = {"CHOWN": 0, "DAC_OVERRIDE": 1, "FOWNER": 3, "KILL": 5}

class Header(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

class Data(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]

def drop(bit):
    libc = ctypes.CDLL(None, use_errno=True)
    header, data = Header(0x20080522, 0), (Data * 2)()
    if libc.capget(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "capget")
    data[0].effective &= ~(1 << bit)
    if libc.capset(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "capset")

cap, operation, target = sys.argv[1:]
if cap != "none":
    drop(BITS[cap])
try:
    if operation == "chown":
        os.chown(target, 1000, 1000)
    elif operation == "list":
        os.listdir(target)
    elif operation == "chmod":
        os.chmod(target, 0o600)
    elif operation == "signal":
        os.kill(int(target), 0)
    print("allowed")
except PermissionError:
    print("denied")
"""

CAPABILITY_CASES = [
    # (capability, operation, setup as root, setup as student)
    ("CHOWN", "chown", "touch /tmp/root-owned && echo /tmp/root-owned", None),
    (
        "DAC_OVERRIDE",
        "list",
        None,
        "mkdir -p ~/locked && touch ~/locked/file && chmod 000 ~/locked && echo ~/locked",
    ),
    ("FOWNER", "chmod", None, "touch ~/student-file && echo ~/student-file"),
    ("KILL", "signal", None, "sleep 300 >/dev/null 2>&1 & echo $!"),
]


@pytest.mark.parametrize(
    ("capability", "operation", "root_setup", "student_setup"),
    CAPABILITY_CASES,
    ids=[case[0] for case in CAPABILITY_CASES],
)
async def test_platform_capability_is_required(
    runtime: DockerRuntime,
    fresh_lab: ContainerInfo,
    capability: str,
    operation: str,
    root_setup: str | None,
    student_setup: str | None,
) -> None:
    if root_setup:
        setup = await sh(runtime, fresh_lab, root_setup, user="root")
    else:
        assert student_setup is not None
        setup = await sh(runtime, fresh_lab, student_setup)
    assert setup.exit_code == 0, setup.stderr
    target = setup.stdout.decode().strip()

    async def probe_as_root(dropped: str) -> bytes:
        result = await runtime.exec(
            fresh_lab.id,
            ["python3", "-I", "-c", CAPABILITY_PROBE, dropped, operation, target],
            user="root",
            time_limit=10,
        )
        assert result.exit_code == 0, result.stderr
        return result.stdout.strip()

    assert await probe_as_root(capability) == b"denied"
    assert await probe_as_root("none") == b"allowed"


# Filesystem


async def test_rootfs_is_read_only_even_for_root(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    for path in ("/usr/bin/linuxlab-test", "/etc/linuxlab-test", "/opt/labctl/linuxlab-test"):
        result = await sh(runtime, lab, f"touch {path}", user="root")
        assert result.exit_code != 0, path
        assert b"Read-only file system" in result.stderr, (path, result.stderr)


async def test_platform_directory_is_closed_to_student(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    student = await sh(runtime, lab, "ls /opt/labctl")
    root = await sh(runtime, lab, "ls /opt/labctl", user="root")

    assert student.exit_code != 0
    assert root.exit_code == 0


async def test_student_cannot_modify_system_binaries(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    result = await sh(runtime, lab, "echo x >> /usr/bin/python3")

    assert result.exit_code != 0


async def test_student_writes_only_to_expected_locations(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    for directory in ("/home/student", "/tmp", "/run/lab"):
        result = await sh(runtime, lab, f"touch {directory}/probe && rm {directory}/probe")
        assert result.exit_code == 0, (directory, result.stderr)
    for directory in ("/", "/etc", "/opt", "/usr/local", "/var/tmp", "/root"):
        result = await sh(runtime, lab, f"touch {directory}/probe")
        assert result.exit_code != 0, directory


async def test_tmpfs_ownership_and_mode(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, "stat -c '%n %u %g %a' /home/student /tmp /run/lab")

    assert result.stdout.decode().splitlines() == [
        "/home/student 1000 1000 755",
        "/tmp 0 0 1777",
        "/run/lab 1000 1000 755",
    ]


async def test_tmpfs_sizes(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, "df -k --output=target,size /home/student /tmp /run/lab")

    sizes = dict(line.split() for line in result.stdout.decode().splitlines()[1:])
    assert sizes == {"/home/student": "65536", "/tmp": "32768", "/run/lab": "1024"}


async def test_tmpfs_mount_options(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    mounts = (await sh(runtime, lab, "cat /proc/mounts")).stdout.decode()

    for target, executable in (("/home/student", True), ("/tmp", True), ("/run/lab", False)):
        entries = [line.split() for line in mounts.splitlines() if line.split()[1] == target]
        assert len(entries) == 1, (target, entries)
        _source, _target, fstype, raw_options, *_ = entries[0]
        options = set(raw_options.split(","))
        assert fstype == "tmpfs", target
        assert {"rw", "nosuid"} <= options, (target, raw_options)
        assert ("noexec" not in options) == executable, (target, raw_options)
        # gVisor does not report nodev; device creation is checked by behavior below.
        if OCI_RUNTIME != "runsc":
            assert "nodev" in options, (target, raw_options)


SCRIPT = "printf '#!/bin/sh\\necho ran\\n' > {path} && chmod +x {path} && {path}"


@pytest.mark.parametrize(
    ("directory", "executable"),
    [("/home/student", True), ("/tmp", True), ("/run/lab", False)],
)
async def test_script_execution_by_location(
    runtime: DockerRuntime, lab: ContainerInfo, directory: str, executable: bool
) -> None:
    path = f"{directory}/exec-probe.sh"
    result = await sh(
        runtime, lab, SCRIPT.format(path=path) + f"; status=$?; rm -f {path}; exit $status"
    )

    if executable:
        assert result.exit_code == 0, result
        assert result.stdout == b"ran\n"
    else:
        assert result.exit_code == 126, result
        assert b"Permission denied" in result.stderr


@pytest.mark.parametrize("user", ["student", "root"])
@pytest.mark.parametrize("directory", ["/home/student", "/tmp"])
async def test_device_nodes_cannot_be_created(
    runtime: DockerRuntime, lab: ContainerInfo, user: ExecUser, directory: str
) -> None:
    # c 1 1 is /dev/mem.
    result = await sh(runtime, lab, f"mknod {directory}/mem c 1 1", user=user)

    assert result.exit_code != 0, result
    assert b"Operation not permitted" in result.stderr


async def test_home_size_is_enforced(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    fill = await sh(runtime, lab, "dd if=/dev/zero of=$HOME/fill bs=1M count=100 2>&1")
    size = await sh(runtime, lab, "stat -c %s $HOME/fill; rm -f $HOME/fill")

    assert fill.exit_code != 0
    assert b"No space left on device" in fill.stdout
    assert int(size.stdout.split()[0]) <= 64 * 1024 * 1024


# Network


async def test_only_loopback_interface(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, "ip -o link show | awk -F': ' '{print $2}'")

    assert result.stdout.decode().split() == ["lo"]


async def test_localhost_is_available(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    script = (
        "import socket\n"
        "server = socket.create_server(('127.0.0.1', 0))\n"
        "client = socket.create_connection(server.getsockname(), timeout=3)\n"
        "connection, _ = server.accept()\n"
        "client.sendall(b'ping')\n"
        "print(connection.recv(4).decode())\n"
    )
    result = await runtime.exec(
        lab.id, ["python3", "-I", "-c", script], user="student", time_limit=10
    )

    assert result.stdout == b"ping\n", result


async def test_no_outbound_connectivity(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    script = (
        "import socket\n"
        "for host, port in [('1.1.1.1', 443), ('8.8.8.8', 53), ('169.254.169.254', 80)]:\n"
        "    try:\n"
        "        socket.create_connection((host, port), timeout=3).close()\n"
        "        print('connected', host)\n"
        "    except OSError:\n"
        "        print('blocked', host)\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 443)\n"
        "    print('resolved')\n"
        "except OSError:\n"
        "    print('unresolved')\n"
    )
    result = await runtime.exec(
        lab.id, ["python3", "-I", "-c", script], user="student", time_limit=20
    )

    assert result.stdout.decode().split("\n")[:4] == [
        "blocked 1.1.1.1",
        "blocked 8.8.8.8",
        "blocked 169.254.169.254",
        "unresolved",
    ]


async def test_network_mode_is_none(docker_client: aiodocker.Docker, lab: ContainerInfo) -> None:
    details = await docker_inspect(docker_client, lab)

    assert details["HostConfig"]["NetworkMode"] == "none"


# Host


async def test_docker_socket_is_absent(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(
        runtime, lab, "ls -la /var/run/docker.sock /run/docker.sock 2>&1", user="root"
    )

    assert result.exit_code != 0
    assert result.stdout.count(b"No such file or directory") == 2


async def test_no_host_mounts(docker_client: aiodocker.Docker, lab: ContainerInfo) -> None:
    details = await docker_inspect(docker_client, lab)

    assert details["Mounts"] == []
    assert not details["HostConfig"]["Binds"]
    assert not details["HostConfig"]["Privileged"]
    assert details["HostConfig"]["PidMode"] in ("", "private")


# Files Docker itself provides in every container. They are managed by the daemon,
# specific to this container, and checked for writability below.
DOCKER_MANAGED_FILES = {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
EXPECTED_MOUNTS = {"/", "/home/student", "/tmp", "/run/lab"} | DOCKER_MANAGED_FILES
KERNEL_FILESYSTEMS = ("/proc", "/sys", "/dev")


async def test_only_expected_mount_points(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    mounts = (await sh(runtime, lab, "cat /proc/mounts")).stdout.decode()
    targets = {line.split()[1] for line in mounts.splitlines()}

    unexpected = {
        target
        for target in targets - EXPECTED_MOUNTS
        if not any(target == fs or target.startswith(f"{fs}/") for fs in KERNEL_FILESYSTEMS)
    }
    assert unexpected == set()


async def test_docker_managed_files_are_not_writable(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    for path in sorted(DOCKER_MANAGED_FILES):
        student = await sh(runtime, lab, f"echo x >> {path}")
        root = await sh(runtime, lab, f"echo x >> {path}", user="root")
        assert student.exit_code != 0, path
        assert root.exit_code != 0, (path, "root could write a file backed by the host")


# Resources


async def test_pids_limit_contains_process_creation(
    runtime: DockerRuntime, docker_client: aiodocker.Docker, fresh_lab: ContainerInfo
) -> None:
    details = await docker_inspect(docker_client, fresh_lab)
    assert details["HostConfig"]["PidsLimit"] == PIDS_LIMIT

    script = (
        "import os, signal, time\n"
        "children, blocked = [], False\n"
        "try:\n"
        "    for _ in range(400):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(60)\n"
        "            os._exit(0)\n"
        "        children.append(pid)\n"
        "except OSError:\n"
        "    blocked = True\n"
        "for pid in children:\n"
        "    os.kill(pid, signal.SIGKILL)\n"
        "print(blocked, len(children))\n"
    )
    result = await runtime.exec(
        fresh_lab.id, ["python3", "-I", "-c", script], user="student", time_limit=60
    )
    blocked, created = result.stdout.split()

    assert blocked == b"True", result
    assert 0 < int(created) < PIDS_LIMIT
    assert (await runtime.inspect(fresh_lab.id)).running


async def test_memory_limit_kills_oversized_allocation(
    runtime: DockerRuntime, docker_client: aiodocker.Docker, fresh_lab: ContainerInfo
) -> None:
    details = await docker_inspect(docker_client, fresh_lab)
    assert details["HostConfig"]["Memory"] == MEMORY_BYTES
    assert details["HostConfig"]["MemorySwap"] == MEMORY_BYTES

    def allocate(mib: int) -> list[str]:
        return ["python3", "-I", "-c", f"data = b'x' * ({mib} * 1024 * 1024)"]

    within = await runtime.exec(fresh_lab.id, allocate(256), user="student", time_limit=60)
    beyond = await runtime.exec(fresh_lab.id, allocate(768), user="student", time_limit=60)

    assert within.exit_code == 0, within
    assert beyond.exit_code == 137, beyond
    assert not beyond.timed_out
    assert (await runtime.inspect(fresh_lab.id)).running


async def test_cpu_quota_limits_a_busy_process(
    runtime: DockerRuntime, docker_client: aiodocker.Docker, fresh_lab: ContainerInfo
) -> None:
    details = await docker_inspect(docker_client, fresh_lab)
    assert details["HostConfig"]["NanoCpus"] == NANO_CPUS

    script = (
        "import time\n"
        "wall, cpu = time.monotonic(), time.process_time()\n"
        "while time.monotonic() - wall < 3:\n"
        "    pass\n"
        "print((time.process_time() - cpu) / (time.monotonic() - wall))\n"
    )
    result = await runtime.exec(
        fresh_lab.id, ["python3", "-I", "-c", script], user="student", time_limit=30
    )
    ratio = float(result.stdout)
    print(f"cpu time / wall time for a busy loop: {ratio:.2f}")

    # A single busy thread would reach ~1.0 without a quota; 0.5 CPU caps it near 0.5.
    assert ratio < 0.75


async def test_seccomp_filter(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    status = (await sh(runtime, lab, "cat /proc/self/status")).stdout
    seccomp = _status_field(status, "Seccomp")
    print(f"Seccomp field in /proc/self/status: {seccomp}")

    if OCI_RUNTIME == "runsc":
        pytest.skip(
            f"gVisor reports Seccomp={seccomp}; syscalls are handled by the gVisor kernel, "
            "which is filtered on the host by gVisor itself, not by the Docker profile"
        )
    assert seccomp == "2"


async def test_latency(runtime: DockerRuntime) -> None:
    started = time.monotonic()
    async with running_lab(runtime) as info:
        ready = time.monotonic()
        await runtime.exec(info.id, ["true"], user="student", time_limit=5)
        executed = time.monotonic()
    print(f"create+start: {ready - started:.2f}s, exec: {executed - ready:.2f}s")

    assert ready - started < 30
    assert executed - ready < 5


# gVisor


@pytest.mark.runsc
async def test_container_runs_under_gvisor(
    runtime: DockerRuntime, docker_client: aiodocker.Docker, lab: ContainerInfo
) -> None:
    details = await docker_inspect(docker_client, lab)
    kernel = await sh(runtime, lab, "uname -r; dmesg", user="root")
    print(kernel.stdout.decode())

    assert details["HostConfig"]["Runtime"] == "runsc"
    assert b"gVisor" in kernel.stdout
