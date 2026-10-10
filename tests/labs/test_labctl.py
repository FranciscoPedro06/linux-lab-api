"""labctl init, the platform utility that prepares a lab's home, in real containers.

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc locally, runsc in the
Runtime workflow).
"""

import pytest

from linuxlab.labs.runtime import ContainerInfo
from linuxlab.labs.runtime.docker import DockerRuntime

from .support import sh

pytestmark = pytest.mark.docker

LABCTL_INIT = ("python3", "-I", "-S", "/opt/labctl/labctl", "init")
SKEL_FILES = (".bash_logout", ".bashrc", ".profile")


async def init(runtime: DockerRuntime, lab: ContainerInfo) -> tuple[int, bytes]:
    result = await runtime.exec(lab.id, LABCTL_INIT, user="root", time_limit=10)
    return result.exit_code, result.stderr


async def test_labctl_init_populates_the_home_from_skel(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    run_lab = await sh(runtime, fresh_lab, "stat -c '%u %g %a' /run/lab")
    assert (await sh(runtime, fresh_lab, "ls -A ~")).stdout == b""

    assert await init(runtime, fresh_lab) == (0, b"")

    listing = await sh(
        runtime, fresh_lab, "cd ~ && stat -c '%n %u %g %a' .* | grep -v '^\\.\\.\\? '"
    )
    skel = await sh(
        runtime, fresh_lab, "cd /etc/skel && stat -c '%n %a' .* | grep -v '^\\.\\.\\? '"
    )
    owned = {line.split()[0]: line.split()[1:] for line in listing.stdout.decode().splitlines()}
    modes = {line.split()[0]: line.split()[1] for line in skel.stdout.decode().splitlines()}
    assert set(owned) == set(modes) == set(SKEL_FILES)
    for name in SKEL_FILES:
        assert owned[name] == ["1000", "1000", modes[name]], name
        same = await sh(runtime, fresh_lab, f"cmp ~/{name} /etc/skel/{name}")
        assert same.exit_code == 0, name
    # The home directory and /run/lab are left as the runtime made them.
    assert (await sh(runtime, fresh_lab, "stat -c '%u %g %a' ~")).stdout == b"1000 1000 755\n"
    assert (await sh(runtime, fresh_lab, "stat -c '%u %g %a' /run/lab")).stdout == run_lab.stdout


async def test_labctl_init_is_idempotent(runtime: DockerRuntime, fresh_lab: ContainerInfo) -> None:
    assert await init(runtime, fresh_lab) == (0, b"")
    first = await sh(runtime, fresh_lab, "cd ~ && sha256sum .bashrc .profile .bash_logout")

    assert await init(runtime, fresh_lab) == (0, b"")
    assert (
        await sh(runtime, fresh_lab, "cd ~ && sha256sum .bashrc .profile .bash_logout")
    ) == first


async def test_labctl_init_does_not_follow_symbolic_links(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    await sh(runtime, fresh_lab, "ln -s /tmp/target ~/.bashrc")

    code, stderr = await init(runtime, fresh_lab)

    assert code == 1
    assert b"not a regular file" in stderr
    assert (await sh(runtime, fresh_lab, "test -e /tmp/target")).exit_code != 0


async def test_student_cannot_read_or_run_labctl(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    for command in (
        "cat /opt/labctl/labctl",
        "ls /opt/labctl",
        "python3 -I -S /opt/labctl/labctl init",
        "test -r /opt/labctl/labctl",
    ):
        result = await sh(runtime, lab, command)
        assert result.exit_code != 0, command
    assert (await sh(runtime, lab, "touch /opt/labctl/x", user="root")).exit_code != 0
