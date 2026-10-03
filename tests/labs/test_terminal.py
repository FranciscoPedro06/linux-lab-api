"""End to end: WebSocket -> FastAPI -> docker exec with a PTY -> bash in a real lab.

The lab is created through the lab lifecycle and the terminal is opened with a real
session cookie. The terminal echoes what is typed, so each check waits for text only
the shell can produce, such as the result of `$((1+1))`, never for text that was sent.
"""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import aiodocker
import pytest
from websockets.asyncio.client import ClientConnection

from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime.docker import DockerRuntime

from .app_support import Account, Api, running_api
from .support import LAB_IMAGE, OCI_RUNTIME, processes
from .terminal_client import close_code, next_control, read_until, start
from .terminal_client import terminal as open_terminal

pytestmark = [pytest.mark.docker, pytest.mark.integration]


@dataclass
class Lab:
    api: Api
    account: Account
    session: LabSession

    @property
    def id(self) -> str:
        """The container id, for inspecting the lab from the tests."""
        return f"ll-lab-{self.session.lab_key}"

    def terminal(self) -> AbstractAsyncContextManager[ClientConnection]:
        return open_terminal(self.api.url, str(self.session.id), self.account.token)


@pytest.fixture(scope="module")
async def api(docker_client: aiodocker.Docker) -> AsyncIterator[Api]:
    runtime = DockerRuntime(docker_client, oci_runtime=OCI_RUNTIME)
    async with running_api(
        runtime,
        lab_oci_runtime=OCI_RUNTIME,
        lab_image=LAB_IMAGE,
        lab_deployment=f"tests-{uuid.uuid4().hex[:12]}",
    ) as api:
        yield api


@pytest.fixture(scope="module")
async def lab(api: Api) -> AsyncIterator[Lab]:
    account = await api.account("ana@example.com")
    session = await api.lab(account)
    yield Lab(api, account, session)
    await api.end(session)


@pytest.fixture(scope="module")
def runtime(api: Api) -> DockerRuntime:
    docker_runtime: DockerRuntime = api.app.state.runtime
    return docker_runtime


async def open_shell(websocket: ClientConnection, cols: int = 80, rows: int = 24) -> None:
    """Start the terminal and wait for the prompt, as a person would before typing.

    Input that arrives before bash has set up line editing can be discarded by bash.
    """
    await start(websocket, cols=cols, rows=rows)
    await read_until(websocket, b"$ ")


ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def lines(output: bytes) -> list[str]:
    """Output as text lines, without escape sequences or carriage returns."""
    text = ESCAPE_SEQUENCE.sub("", output.decode(errors="replace"))
    return [line.strip("\r") for line in text.split("\n")]


async def run(websocket: ClientConnection, command: str, done: str) -> bytes:
    """Type a command followed by `echo <done>$((1))` and wait for `<done>1`."""
    await websocket.send(f"{command}; echo {done}$((1))\r".encode())
    return await read_until(websocket, f"{done}1\r\n".encode())


async def test_commands_run_in_the_lab_shell(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)

        assert b"/home/student\r\n" in await run(websocket, "pwd", "a")
        assert b"student 1000\r\n" in await run(websocket, 'echo "$(id -un) $(id -u)"', "b")
        await run(websocket, "mkdir -p projeto && cd projeto && touch teste.txt", "c")
        output = await run(websocket, "pwd; ls", "d")
        assert b"/home/student/projeto\r\n" in output
        assert b"teste.txt\r\n" in output
        await run(websocket, "cd /tmp", "e")
        assert b"/tmp\r\n" in await run(websocket, "pwd", "f")
        await run(websocket, "GREETING=ola", "g")
        assert b"ola\r\n" in await run(websocket, "echo $GREETING", "h")


async def test_ctrl_c_interrupts_the_foreground_command(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        await websocket.send(b"sleep 30\r")
        await asyncio.sleep(0.5)
        await websocket.send(b"\x03")

        output = await run(websocket, "echo status=$?", "after")
    assert b"status=130\r\n" in output


async def test_ctrl_c_stops_a_flood_of_output(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        await websocket.send(b"yes\r")
        await read_until(websocket, b"y\r\n" * 1000)
        await websocket.send(b"\x03")

        await run(websocket, "true", "stopped")


async def test_ctrl_d_ends_the_shell(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        await websocket.send(b"\x04")

        assert await next_control(websocket) == {"type": "exit", "code": 0}
        assert await close_code(websocket) == 4000


async def test_line_editing_and_history(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        # Backspace (DEL) removes the X before the line runs.
        assert b"abc1\r\n" in await run(websocket, "echo abX\x7fc$((1))", "bs")
        # Up arrow recalls the previous command.
        await run(websocket, "echo recall$((2))", "hist")
        await websocket.send(b"\x1b[A\r")
        assert b"recall2\r\n" in await read_until(websocket, b"recall2\r\n")


async def test_interactive_program_reads_input(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        await websocket.send(b"read -r name; echo hi-$name-$((1))\r")
        await asyncio.sleep(0.3)
        await websocket.send(b"maria\r")
        assert b"hi-maria-1\r\n" in await read_until(websocket, b"hi-maria-1\r\n")


async def test_init_size_and_resize_reach_the_pty(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket, cols=90, rows=25)
        assert b"25 90\r\n" in await run(websocket, "stty size", "init")

        await websocket.send(json.dumps({"type": "resize", "cols": 120, "rows": 32}))
        assert b"32 120\r\n" in await run(websocket, "stty size", "resized")


async def test_large_output_arrives_complete(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        output = await run(websocket, "head -c 300000 /dev/zero | tr '\\0' a; echo", "big")
    assert output.count(b"a") >= 300000


async def test_terminal_stays_unprivileged_and_isolated(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        output = await run(
            websocket,
            "id -u; test -e /var/run/docker.sock || echo no-socket; "
            "command -v sudo || echo no-sudo; ip -o link show | awk -F': ' '{print $2}'",
            "checks",
        )
    result = lines(output)
    assert "1000" in result
    assert "no-socket" in result
    assert "no-sudo" in result
    assert "lo" in result
    assert "eth0" not in result


async def test_two_users_labs_do_not_share_state(api: Api, lab: Lab) -> None:
    bia = await api.account("bia@example.com")
    other = Lab(api, bia, await api.lab(bia))
    try:
        async with lab.terminal() as first:
            await open_shell(first)
            await run(first, "touch ~/only-in-first", "made")
        async with other.terminal() as second:
            await open_shell(second)
            output = await run(second, "ls ~/only-in-first 2>&1", "looked")
        # Neither user can open the other's terminal.
        async with open_terminal(api.url, str(other.session.id), lab.account.token) as foreign:
            assert await close_code(foreign) == 4404
    finally:
        await api.end(other.session)
    assert b"No such file or directory" in output


@pytest.mark.parametrize("ending", ["close", "abort"])
async def test_disconnect_ends_the_shell_and_its_processes(
    runtime: DockerRuntime, lab: Lab, ending: str
) -> None:
    async with lab.terminal() as websocket:
        await open_shell(websocket)
        await websocket.send(b"sleep 341 & sleep 342\r")
        await asyncio.sleep(1)
        assert len(await processes(runtime, lab, "sleep 34[12]")) == 2
        if ending == "abort":
            websocket.transport.abort()

    async with asyncio.timeout(15):
        while await processes(runtime, lab, "sleep 34[12]|bash --login"):
            await asyncio.sleep(0.2)
