import asyncio
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
    TerminalSize,
)
from linuxlab.labs.runtime.docker import MAX_OUTPUT_BYTES, DockerRuntime

from .support import LAB_IMAGE, OCI_RUNTIME, processes, read_until, sh

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


# Interactive terminals


async def test_terminal_is_a_login_shell_on_a_pty_as_student(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    terminal = await runtime.open_terminal(lab.id, TerminalSize(cols=80, rows=24))
    try:
        await terminal.write(
            b"id -un; [ -t 0 ] && echo stdin-is-a-terminal; tty; stty size; echo $TERM\r"
        )
        output = await read_until(terminal, b"xterm-256color\r\n")
    finally:
        await terminal.close()

    assert b"student\r\n" in output
    assert b"stdin-is-a-terminal\r\n" in output
    assert b"24 80\r\n" in output
    if OCI_RUNTIME == "runsc":
        # gVisor hands the exec a host terminal: it is a TTY (isatty, window size, line
        # editing) but has no /dev/pts name, so ttyname() and `tty` fail.
        assert b"not a tty\r\n" in output
    else:
        assert b"/dev/pts/" in output


async def test_terminal_resize_reaches_the_pty(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    terminal = await runtime.open_terminal(lab.id, TerminalSize(cols=80, rows=24))
    try:
        await terminal.resize(TerminalSize(cols=132, rows=40))
        await terminal.write(b"stty size\r")
        output = await read_until(terminal, b"40 132\r\n")
    finally:
        await terminal.close()

    assert b"40 132" in output


async def test_terminal_reports_the_shell_exit_code(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    terminal = await runtime.open_terminal(lab.id, TerminalSize(cols=80, rows=24))
    await terminal.write(b"exit 3\r")
    async with asyncio.timeout(10):
        while await terminal.read() is not None:
            pass

    assert await terminal.close() == 3
    assert await terminal.close() == 3


async def test_closing_a_terminal_ends_its_processes(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    terminal = await runtime.open_terminal(lab.id, TerminalSize(cols=80, rows=24))
    await terminal.write(b"sleep 311 & setsid sleep 312 & sleep 313\r")
    await asyncio.sleep(1)
    assert len(await processes(runtime, lab, "sleep 31[123]")) == 3

    await terminal.close()

    assert await processes(runtime, lab, "sleep 31[123]") == []
    assert await processes(runtime, lab, "bash --login") == []


async def test_closing_a_terminal_leaves_other_terminals_running(
    runtime: DockerRuntime, lab: ContainerInfo
) -> None:
    first = await runtime.open_terminal(lab.id, TerminalSize(cols=80, rows=24))
    second = await runtime.open_terminal(lab.id, TerminalSize(cols=80, rows=24))
    try:
        await first.write(b"sleep 321\r")
        await second.write(b"sleep 322 &\r")
        await asyncio.sleep(1)

        await first.close()

        assert await processes(runtime, lab, "sleep 321") == []
        assert len(await processes(runtime, lab, "sleep 322")) == 1
        await second.write(b"echo still-here\r")
        assert b"still-here" in await read_until(second, b"still-here\r\n")
    finally:
        await first.close()
        await second.close()


@pytest.mark.parametrize("ending", ["stop", "remove"])
async def test_closing_a_terminal_after_its_lab_ended_is_not_an_error(
    runtime: DockerRuntime, ending: str
) -> None:
    info = await runtime.create(LabContainerSpec(lab_id=uuid.uuid4().hex, image=LAB_IMAGE))
    try:
        await runtime.start(info.id)
        terminal = await runtime.open_terminal(info.id, TerminalSize(cols=80, rows=24))
        await terminal.write(b"sleep 351\r")
        if ending == "stop":
            await runtime.stop(info.id)
        else:
            await runtime.remove(info.id)

        await terminal.close()
    finally:
        await runtime.remove(info.id)


async def test_terminal_requires_a_running_container(runtime: DockerRuntime) -> None:
    info = await runtime.create(LabContainerSpec(lab_id=uuid.uuid4().hex, image=LAB_IMAGE))
    try:
        with pytest.raises(ContainerNotRunningError):
            await runtime.open_terminal(info.id, TerminalSize(cols=80, rows=24))
    finally:
        await runtime.remove(info.id)
