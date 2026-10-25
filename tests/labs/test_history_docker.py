"""Command history end to end: bash in a real lab, the hook in the lab image, the relay
and PostgreSQL, under the runtime of LINUXLAB_TEST_OCI_RUNTIME (runc or runsc).

Each test types into the real terminal and checks what the lab's history holds
afterwards. What the history leaves out is checked as carefully as what it keeps.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import aiodocker
import httpx2
import pytest
from websockets.asyncio.client import ClientConnection

from linuxlab.auth.cookies import SESSION_COOKIE
from linuxlab.history.marker import MARKER_PREFIX
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime.docker import DockerRuntime

from .app_support import Account, Api, running_api
from .support import LAB_IMAGE, OCI_RUNTIME
from .terminal_client import read_until, start
from .terminal_client import terminal as open_terminal

pytestmark = [pytest.mark.docker, pytest.mark.integration]


@dataclass
class Lab:
    api: Api
    account: Account
    session: LabSession

    def terminal(self) -> AbstractAsyncContextManager[ClientConnection]:
        return open_terminal(self.api.url, str(self.session.id), self.account.token)

    async def history(self) -> list[str]:
        """The lab's entries, once every closed connection has written what it queued
        (the server runs on this event loop, so its writer tasks are visible here)."""
        await writers_finished()
        rows = await self.api.sql(
            "SELECT command FROM command_history WHERE lab_session_id = :lab ORDER BY seq",
            lab=self.session.id,
        )
        return [command for (command,) in rows]


async def writers_finished(seconds: float = 10) -> None:
    async with asyncio.timeout(seconds):
        while any(task.get_name() == "terminal-history" for task in asyncio.all_tasks()):
            await asyncio.sleep(0.02)


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


@pytest.fixture
async def lab(api: Api) -> AsyncIterator[Lab]:
    account = await api.account(f"{uuid.uuid4().hex[:8]}@example.com")
    session = await api.lab(account)
    yield Lab(api, account, session)
    await api.end(session)


class Shell:
    """A terminal whose output is checked to never carry a marker."""

    def __init__(self, websocket: ClientConnection) -> None:
        self.websocket = websocket
        self.output = b""

    async def until(self, needle: bytes, seconds: float = 15) -> bytes:
        output = await read_until(self.websocket, needle, seconds)
        self.output += output
        assert MARKER_PREFIX not in self.output
        return output

    async def send(self, data: bytes) -> None:
        await self.websocket.send(data)

    async def run(self, line: str, done: str) -> None:
        """Type a line, then a separate `echo` whose output shows the line finished."""
        await self.send(f"{line}\r".encode())
        await self.send(f"echo {done}$((1))\r".encode())
        await self.until(f"{done}1\r\n".encode())


async def shell(websocket: ClientConnection) -> Shell:
    await start(websocket)
    opened = Shell(websocket)
    await opened.until(b"$ ")
    return opened


def without_markers(entries: list[str]) -> list[str]:
    """The entries other than the `echo <done>$((1))` lines the tests add."""
    return [entry for entry in entries if not entry.startswith("echo done")]


async def test_simple_and_compound_command_lines(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.run("echo simple", "done-a")
        await sh.run("echo a; echo b && echo c || echo d", "done-b")
        await sh.run("ls / | wc -l > /tmp/count && cat < /tmp/count", "done-c")

    assert without_markers(await lab.history()) == [
        "echo simple",
        "echo a; echo b && echo c || echo d",
        "ls / | wc -l > /tmp/count && cat < /tmp/count",
    ]


async def test_multiline_lines_are_one_entry(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"for i in 1 2\r")
        await sh.send(b"do echo n$i\r")
        await sh.send(b"done\r")
        await sh.until(b"n2\r\n")
        await sh.send(b"echo 'first\r")
        await sh.send(b"second'\r")
        await sh.until(b"second\r\n")
        await sh.run("true", "done-a")

    assert without_markers(await lab.history()) == [
        "for i in 1 2; do echo n$i; done",
        "echo 'first\nsecond'",
        "true",
    ]


async def test_the_hook_keeps_the_exit_status_and_last_argument(lab: Lab) -> None:
    """PS0 runs before each command; $? and $_ still refer to the previous command."""
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"false\r")
        await sh.send(b"echo status=$?\r")
        await sh.until(b"status=1\r\n")
        await sh.send(b"echo lastarg\r")
        await sh.send(b"echo underscore=$_\r")
        await sh.until(b"underscore=lastarg\r\n")

    assert await lab.history() == [
        "false",
        "echo status=$?",
        "echo lastarg",
        "echo underscore=$_",
    ]


async def test_lines_bash_does_not_run_or_keep_are_left_out(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"\r")
        await sh.send(b"\r")
        await sh.send(b" echo hidden$((1))\r")
        await sh.until(b"hidden1\r\n")
        await sh.send(b"if then fi\r")
        await sh.until(b"syntax error")
        await sh.run("true", "done-a")

    assert without_markers(await lab.history()) == ["true"]


async def test_a_line_cancelled_with_ctrl_c_is_neither_run_nor_recorded(api: Api, lab: Lab) -> None:
    """Nothing is typed after Ctrl+C: under runsc, input sent right after the prompt
    that follows Ctrl+C can lose characters (docs/terminal.md, Known behavior), which
    has nothing to do with the history. The history is read through the API."""
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.run("echo before", "done-a")
        await sh.send(b"touch /tmp/cancelled-line")
        await sh.send(b"\x03")
        after = (await sh.until(b"^C")).split(b"^C")[-1]
        if b"$ " not in after:
            await sh.until(b"$ ")

    await writers_finished()
    base_url = api.url.replace("ws://", "http://", 1)
    async with httpx2.AsyncClient(base_url=base_url) as http:
        response = await http.get(
            f"/api/labs/{lab.session.id}/history",
            headers={"cookie": f"{SESSION_COOKIE}={lab.account.token}"},
        )
    assert response.status_code == 200, response.text
    commands = [entry["command"] for entry in response.json()["entries"]]
    assert commands == ["echo before", "echo done-a$((1))"]

    runtime: DockerRuntime = api.app.state.runtime
    result = await runtime.exec(
        f"ll-lab-{lab.session.lab_key}",
        ["sh", "-c", "test -e /tmp/cancelled-line && echo exists || echo absent"],
        user="student",
        time_limit=10,
    )
    assert result.stdout == b"absent\n"


async def test_repeated_lines_are_separate_entries(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.run("echo again", "done-a")
        await sh.send(b"echo again\r")
        await sh.send(b"echo again\r")
        await sh.run("true", "done-b")

    assert await lab.history() == [
        "echo again",
        "echo done-a$((1))",
        "echo again",
        "echo again",
        "true",
        "echo done-b$((1))",
    ]


async def test_aliases_are_kept_as_typed_and_history_expansion_as_expanded(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"alias greet='echo aliased'\r")
        await sh.run("greet", "done-a")
        await sh.send(b"echo expand$((2))\r")
        await sh.until(b"expand2\r\n")
        await sh.send(b"!!\r")
        await sh.until(b"expand2\r\n")
        await sh.send(b"\x1b[A\r")  # the up arrow recalls the same line
        await sh.until(b"expand2\r\n")
        await sh.run("true", "done-b")

    assert without_markers(await lab.history()) == [
        "alias greet='echo aliased'",
        "greet",
        "echo expand$((2))",
        "echo expand$((2))",
        "echo expand$((2))",
        "true",
    ]


async def test_input_read_by_programs_is_never_recorded(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"read -rs pw; echo len=${#pw}\r")
        await asyncio.sleep(0.5)
        await sh.send(b"hunter2-secret\r")
        await sh.until(b"len=14\r\n")
        await sh.send(b"python3 -c 'print(\"got-\" + input())'\r")
        await asyncio.sleep(0.5)
        await sh.send(b"typed-into-python\r")
        await sh.until(b"got-typed-into-python\r\n")
        await sh.run("true", "done-a")

    entries = await lab.history()
    assert without_markers(entries) == [
        "read -rs pw; echo len=${#pw}",
        "python3 -c 'print(\"got-\" + input())'",
        "true",
    ]
    assert not [entry for entry in entries if "hunter2" in entry or "typed-into" in entry]


async def test_a_heredoc_is_recorded_with_its_body(lab: Lab) -> None:
    """Documented: what is typed at the prompt, heredoc bodies included, is kept."""
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"cat <<EOF > /tmp/note\r")
        await sh.send(b"body-line\r")
        await sh.send(b"EOF\r")
        await sh.run("cat /tmp/note", "done-a")

    assert without_markers(await lab.history()) == [
        "cat <<EOF > /tmp/note\nbody-line\nEOF",
        "cat /tmp/note",
    ]


async def test_interactive_nested_bash_is_recorded_but_scripts_are_not(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.send(b"printf 'echo from-script\\n' > /tmp/s.sh\r")
        await sh.run("bash /tmp/s.sh; bash -c 'echo from-c'", "done-a")
        await sh.send(b"bash\r")
        await sh.run("echo inner", "done-b")
        await sh.send(b"exit\r")
        await sh.run("sh -c 'echo from-sh'", "done-c")

    assert without_markers(await lab.history()) == [
        "printf 'echo from-script\\n' > /tmp/s.sh",
        "bash /tmp/s.sh; bash -c 'echo from-c'",
        "bash",
        "echo inner",
        "exit",
        "sh -c 'echo from-sh'",
    ]


async def test_a_line_over_the_limit_is_left_out_whole(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        long = "echo " + "x" * 4100
        await sh.send(long.encode() + b" > /dev/null\r")
        await sh.run("true", "done-a")

    assert without_markers(await lab.history()) == ["true"]


async def test_a_marker_written_by_the_student_is_recorded(lab: Lab) -> None:
    """A known limitation: anything in the lab can write a marker. The forged entry
    lands only in the student's own history, which proves nothing."""
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.run(r"printf '\033]7771;%s\007' $(printf forged | base64)", "done-a")

    assert without_markers(await lab.history()) == [
        r"printf '\033]7771;%s\007' $(printf forged | base64)",
        "forged",
    ]


async def test_turning_the_hook_off_stops_the_history(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.run("PS0=", "done-a")
        await sh.run("echo unrecorded", "done-b")

    assert await lab.history() == ["PS0="]


async def test_reconnecting_continues_the_lab_history(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        await (await shell(websocket)).run("echo first", "done-a")
    async with lab.terminal() as websocket:
        await (await shell(websocket)).run("echo second", "done-b")

    assert without_markers(await lab.history()) == ["echo first", "echo second"]


@pytest.mark.parametrize("mission", ["sample-file", "sample-answer"])
async def test_a_replacement_starts_with_an_empty_history(lab: Lab, mission: str) -> None:
    async with lab.terminal() as websocket:
        await (await shell(websocket)).run("echo old-lab", "done-a")
    creation = await lab.api.labs.replace(
        lab.session.id, mission, session_id=lab.account.session_id, admit=lambda: None
    )
    successor = Lab(lab.api, lab.account, creation.lab)
    try:
        assert without_markers(await lab.history()) == ["echo old-lab"]
        assert await successor.history() == []
        async with successor.terminal() as websocket:
            await (await shell(websocket)).run("echo new-lab", "done-b")
        assert without_markers(await successor.history()) == ["echo new-lab"]
        assert without_markers(await lab.history()) == ["echo old-lab"]
    finally:
        await lab.api.end(creation.lab)


async def test_commands_before_the_lab_ends_are_kept(lab: Lab) -> None:
    async with lab.terminal() as websocket:
        sh = await shell(websocket)
        await sh.run("echo before-end", "done-a")
        await lab.api.end(lab.session)

    assert await lab.api.status(lab.session) == ("terminated", "user")
    assert without_markers(await lab.history()) == ["echo before-end"]


async def test_non_interactive_shells_do_not_load_the_hook(api: Api, lab: Lab) -> None:
    runtime: DockerRuntime = api.app.state.runtime
    container = f"ll-lab-{lab.session.lab_key}"
    script = 'printf \'%s|\' "${PS0-unset}" "$(type -t __linuxlab_history_emit)"; echo ok'
    for argv in (["bash", "-c", script], ["bash", "--login", "-c", script]):
        result = await runtime.exec(container, argv, user="student", time_limit=10)
        assert result.exit_code == 0, result.stderr
        assert result.stdout == b"unset||ok\n"
        assert MARKER_PREFIX not in result.stdout + result.stderr
    assert await lab.history() == []


async def test_the_hook_cannot_be_changed_by_the_student(api: Api, lab: Lab) -> None:
    runtime: DockerRuntime = api.app.state.runtime
    container = f"ll-lab-{lab.session.lab_key}"
    result = await runtime.exec(
        container,
        ["sh", "-c", "stat -c '%U %a' /etc/linuxlab/history.bash; echo x >> /etc/bash.bashrc"],
        user="student",
        time_limit=10,
    )
    assert result.stdout == b"root 644\n"
    assert result.exit_code != 0
