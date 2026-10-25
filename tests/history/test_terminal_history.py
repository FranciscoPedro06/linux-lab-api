"""History through the terminal WebSocket, against PostgreSQL and a FakeRuntime.

The fake terminal echoes its input, so a test makes the "shell" write a marker by
sending it as input.
"""

import asyncio
import base64
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from websockets.asyncio.client import ClientConnection

from linuxlab.history.marker import MARKER_END, MARKER_PREFIX
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime.fake import FakeRuntime, FakeTerminalSession
from tests.labs.app_support import Account, Api, running_api
from tests.labs.terminal_client import close_code, read_until, start, terminal

pytestmark = pytest.mark.integration


def marker(command: str) -> bytes:
    return MARKER_PREFIX + base64.b64encode(command.encode()) + MARKER_END


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with running_api(FakeRuntime()) as api:
        yield api


@pytest.fixture
async def ana(api: Api) -> Account:
    return await api.account("ana@example.com")


@pytest.fixture
async def lab(api: Api, ana: Account) -> LabSession:
    return await api.lab(ana)


def last_terminal(api: Api) -> FakeTerminalSession:
    fake = api.app.state.runtime
    assert isinstance(fake, FakeRuntime)
    return fake.terminals[-1]


async def history(api: Api, lab: LabSession) -> list[tuple[int, str]]:
    return [
        (seq, command)
        for seq, command in await api.sql(
            "SELECT seq, command FROM command_history WHERE lab_session_id = :lab ORDER BY seq",
            lab=lab.id,
        )
    ]


async def wait_for_history(api: Api, lab: LabSession, count: int) -> list[tuple[int, str]]:
    async with asyncio.timeout(5):
        while len(entries := await history(api, lab)) < count:
            await asyncio.sleep(0.02)
    return entries


async def received(websocket: ClientConnection, seconds: float) -> bytes:
    """Everything the client receives within `seconds`."""
    output = b""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        try:
            async with asyncio.timeout(left):
                message = await websocket.recv()
        except TimeoutError:
            break
        assert isinstance(message, bytes)
        output += message
    return output


async def test_markers_are_recorded_and_never_reach_the_browser(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(b"$ ls\r\n" + marker("ls -la") + b"total 0\r\n")
        output = await read_until(websocket, b"total 0\r\n")
        await websocket.send(marker("ls -la") + b"done\r\n")
        output += await read_until(websocket, b"done\r\n")

        assert output == b"$ ls\r\ntotal 0\r\ndone\r\n"
        assert await wait_for_history(api, lab, 2) == [(1, "ls -la"), (2, "ls -la")]


async def test_a_marker_split_across_chunks_is_recorded(
    api: Api, ana: Account, lab: LabSession
) -> None:
    data = b"a" + marker("echo split") + b"b\r\n"
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        for index in range(0, len(data), 3):
            await websocket.send(data[index : index + 3])

        assert await read_until(websocket, b"b\r\n") == b"ab\r\n"
        assert await wait_for_history(api, lab, 1) == [(1, "echo split")]


async def test_bytes_held_for_a_possible_marker_are_passed_on_soon(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(b"abc\x1b]77")

        assert await received(websocket, 1.0) == b"abc\x1b]77"
    assert await history(api, lab) == []


async def test_invalid_markers_are_removed_and_not_recorded(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        too_large = MARKER_PREFIX + base64.b64encode(b"a" * 4098) + MARKER_END
        await websocket.send(MARKER_PREFIX + b"YWJ" + MARKER_END + too_large + b"end\r\n")
        await websocket.send(marker("ok") + b"after\r\n")

        assert await read_until(websocket, b"after\r\n") == b"end\r\nafter\r\n"
        assert await wait_for_history(api, lab, 1) == [(1, "ok")]


async def test_a_failing_database_never_affects_the_terminal(
    api: Api, ana: Account, lab: LabSession, caplog: pytest.LogCaptureFixture
) -> None:
    class Failing:
        async def record(self, lab_id: uuid.UUID, command: str, at: datetime) -> int:
            raise OSError("database unavailable")

    api.app.state.history = Failing()
    with caplog.at_level(logging.INFO, logger="linuxlab"):
        async with terminal(api.url, str(lab.id), ana.token) as websocket:
            await start(websocket)
            for index in range(5):
                await websocket.send(marker(f"echo secret-{index}") + f"out{index}\r\n".encode())
                await read_until(websocket, f"out{index}\r\n".encode())
            await websocket.send(b"still here\r\n")
            await read_until(websocket, b"still here\r\n")
            await websocket.close()

        async with asyncio.timeout(5):
            while "terminal history:" not in caplog.text:
                await asyncio.sleep(0.02)
    assert await history(api, lab) == []
    assert "write_failed=5" in caplog.text
    assert "error=OSError" in caplog.text
    assert "secret" not in caplog.text


async def test_reconnecting_continues_the_history_without_duplicates(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(marker("first") + b"1\r\n")
        await read_until(websocket, b"1\r\n")
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(marker("second") + b"2\r\n")
        await read_until(websocket, b"2\r\n")

    assert await wait_for_history(api, lab, 2) == [(1, "first"), (2, "second")]
    await asyncio.sleep(0.2)
    assert await history(api, lab) == [(1, "first"), (2, "second")]


async def test_a_replaced_connection_keeps_writing_to_its_own_lab_only(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as first:
        await start(first)
        await first.send(marker("before") + b"x\r\n")
        await read_until(first, b"x\r\n")
        creation = await api.labs.replace(
            lab.id, "sample-file", session_id=ana.session_id, admit=lambda: None
        )
        successor = creation.lab
        assert await close_code(first) == 4410

    async with terminal(api.url, str(successor.id), ana.token) as second:
        await start(second)
        await second.send(marker("after") + b"y\r\n")
        await read_until(second, b"y\r\n")

    assert await wait_for_history(api, lab, 1) == [(1, "before")]
    assert await wait_for_history(api, successor, 1) == [(1, "after")]


async def test_ending_a_lab_does_not_wait_for_the_history(
    api: Api, ana: Account, lab: LabSession
) -> None:
    gate = asyncio.Event()

    class Slow:
        async def record(self, lab_id: uuid.UUID, command: str, at: datetime) -> int:
            await gate.wait()
            return 1

    api.app.state.history = Slow()
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(marker("ls") + b"x\r\n")
        await read_until(websocket, b"x\r\n")
        started = time.monotonic()
        await api.end(lab)
        assert await close_code(websocket) == 4410

    assert time.monotonic() - started < 1.5
    assert await api.status(lab) == ("terminated", "user")
    gate.set()


async def test_entries_written_before_the_end_are_kept_after_it(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(marker("cat notes.txt") + b"x\r\n")
        await read_until(websocket, b"x\r\n")
        await api.end(lab)
        assert await close_code(websocket) == 4410

    assert await wait_for_history(api, lab, 1) == [(1, "cat notes.txt")]
    assert await api.status(lab) == ("terminated", "user")
