import time
import uuid

import aiodocker
import pytest

from linuxlab.labs.runtime import (
    ContainerInfo,
    ContainerNotFoundError,
    ContainerNotRunningError,
    LabContainerSpec,
    LabRuntime,
    RuntimeUnavailableError,
)
from linuxlab.labs.runtime.docker import MAX_OUTPUT_BYTES, DockerRuntime

from .support import LAB_IMAGE, sh

pytestmark = pytest.mark.docker


def test_docker_runtime_satisfies_protocol(runtime: DockerRuntime) -> None:
    satisfied: LabRuntime = runtime
    assert satisfied is runtime


async def test_ping_rejects_unregistered_oci_runtime(docker_client: aiodocker.Docker) -> None:
    runtime = DockerRuntime(docker_client, oci_runtime="linuxlab-missing-runtime")

    with pytest.raises(RuntimeUnavailableError):
        await runtime.ping()


async def test_lifecycle(runtime: DockerRuntime) -> None:
    lab_id = uuid.uuid4().hex
    info = await runtime.create(LabContainerSpec(lab_id=lab_id, image=LAB_IMAGE))
    try:
        assert info.name == f"ll-lab-{lab_id}"
        assert info.labels == {"linuxlab.managed": "true", "linuxlab.lab_id": lab_id}
        assert not info.running

        await runtime.start(info.id)
        assert (await runtime.inspect(info.id)).running

        await runtime.stop(info.id)
        assert not (await runtime.inspect(info.id)).running
        with pytest.raises(ContainerNotRunningError):
            await runtime.exec(info.id, ["true"], user="student", time_limit=5)
    finally:
        await runtime.remove(info.id)

    with pytest.raises(ContainerNotFoundError):
        await runtime.inspect(info.id)
    await runtime.remove(info.id)


async def test_exec_captures_output_and_exit_code(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    result = await sh(runtime, lab, "echo out; echo err >&2; exit 3")

    assert result.exit_code == 3
    assert result.stdout == b"out\n"
    assert result.stderr == b"err\n"
    assert not result.timed_out


async def test_exec_does_not_use_a_shell(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await runtime.exec(lab.id, ["echo", "$HOME;id"], user="student", time_limit=5)

    assert result.stdout == b"$HOME;id\n"


async def test_exec_identities(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    student = await runtime.exec(lab.id, ["id", "-u", "-n"], user="student", time_limit=5)
    root = await runtime.exec(lab.id, ["id", "-u"], user="root", time_limit=5)

    assert student.stdout == b"student\n"
    assert root.stdout == b"0\n"


async def test_exec_time_limit_kills_the_process_group(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    started = time.monotonic()
    result = await sh(runtime, lab, "sleep 31 & sleep 32", time_limit=1)
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert elapsed < 10
    leftovers = await sh(runtime, lab, "pgrep -f 'sleep 3[12]' || true")
    assert leftovers.stdout == b""


async def test_exec_output_is_capped(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await sh(runtime, lab, f"head -c {2 * MAX_OUTPUT_BYTES} /dev/zero")

    assert result.exit_code == 0
    assert result.truncated
    assert len(result.stdout) == MAX_OUTPUT_BYTES


async def test_operations_on_missing_container_raise(runtime: DockerRuntime) -> None:
    with pytest.raises(ContainerNotFoundError):
        await runtime.inspect("ll-lab-does-not-exist")
    with pytest.raises(ContainerNotFoundError):
        await runtime.start("ll-lab-does-not-exist")
