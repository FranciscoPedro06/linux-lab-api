"""The order of a lab's history across connections, against PostgreSQL and a FakeRuntime.

A connection's writer can still hold lines when the connection is replaced. The lines
of the next connection must not be stored before them: seq follows the order in which
the relay observed the lines, whichever connection observed them.
"""

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from linuxlab.history.marker import MARKER_END, MARKER_PREFIX
from linuxlab.history.service import CommandHistory
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime.fake import FakeRuntime
from tests.labs.app_support import Account, Api, running_api
from tests.labs.terminal_client import close_code, read_until, start, terminal

pytestmark = pytest.mark.integration


def marker(command: str) -> bytes:
    return MARKER_PREFIX + base64.b64encode(command.encode()) + MARKER_END


class Gated:
    """The real store, with inserts of chosen commands held until released."""

    def __init__(self, store: CommandHistory, held: set[str]) -> None:
        self.store = store
        self.held = held
        self.gate = asyncio.Event()
        self.waiting = 0

    async def record(self, lab_id: uuid.UUID, command: str, at: datetime) -> int:
        if command in self.held:
            self.waiting += 1
            await self.gate.wait()
        return await self.store.record(lab_id, command, at)


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


async def history(api: Api, lab: LabSession) -> list[tuple[int, str]]:
    return [
        (seq, command)
        for seq, command in await api.sql(
            "SELECT seq, command FROM command_history WHERE lab_session_id = :lab ORDER BY seq",
            lab=lab.id,
        )
    ]


async def until(condition: object, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not callable(condition) or not condition():
            await asyncio.sleep(0.02)


async def test_lines_of_a_replaced_connection_stay_before_the_next_ones(
    api: Api, ana: Account, lab: LabSession
) -> None:
    gated = Gated(api.app.state.history, {"A", "B"})
    api.app.state.history = gated
    async with terminal(api.url, str(lab.id), ana.token) as first:
        await start(first)
        await first.send(marker("A") + marker("B") + b"1\r\n")
        await read_until(first, b"1\r\n")
        await until(lambda: gated.waiting == 1)

        # A second connection replaces the first while A and B are not stored yet.
        async with terminal(api.url, str(lab.id), ana.token) as second:
            await start(second)
            assert await close_code(first) == 4409
            await second.send(marker("C") + b"2\r\n")
            await read_until(second, b"2\r\n")
            await asyncio.sleep(0.3)
            gated.gate.set()
            await until(lambda: True)

    async with asyncio.timeout(5):
        while len(entries := await history(api, lab)) < 3:
            await asyncio.sleep(0.02)
    assert entries == [(1, "A"), (2, "B"), (3, "C")]


async def test_a_reconnect_after_a_close_keeps_the_order(
    api: Api, ana: Account, lab: LabSession
) -> None:
    gated = Gated(api.app.state.history, {"A"})
    api.app.state.history = gated
    async with terminal(api.url, str(lab.id), ana.token) as first:
        await start(first)
        await first.send(marker("A") + b"1\r\n")
        await read_until(first, b"1\r\n")
        await until(lambda: gated.waiting == 1)

    async with terminal(api.url, str(lab.id), ana.token) as second:
        await start(second)
        await second.send(marker("C") + b"2\r\n")
        await read_until(second, b"2\r\n")
        await asyncio.sleep(0.3)
        gated.gate.set()

        async with asyncio.timeout(5):
            while len(entries := await history(api, lab)) < 2:
                await asyncio.sleep(0.02)
    assert entries == [(1, "A"), (2, "C")]
