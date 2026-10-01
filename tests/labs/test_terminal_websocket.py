"""Terminal WebSocket behavior against FakeRuntime: protocol, lifecycle and access."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from linuxlab.config import Settings
from linuxlab.labs.runtime import ContainerInfo, LabContainerSpec, LabRuntimeError, TerminalSize
from linuxlab.labs.runtime.fake import FakeRuntime, FakeTerminalSession
from linuxlab.labs.terminal.protocol import MAX_FRAME_BYTES
from linuxlab.main import create_app

from .terminal_client import (
    ORIGIN,
    close_code,
    next_control,
    read_until,
    serve,
    start,
    terminal,
)

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://linuxlab:linuxlab@127.0.0.1:1/linuxlab"


@dataclass
class Server:
    url: str
    runtime: FakeRuntime

    async def lab(self, *, running: bool = True) -> ContainerInfo:
        info = await self.runtime.create(
            LabContainerSpec(lab_id=uuid.uuid4().hex, image="test", deployment="tests")
        )
        if running:
            await self.runtime.start(info.id)
        return info

    def last_terminal(self) -> FakeTerminalSession:
        return self.runtime.terminals[-1]


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": UNREACHABLE_DATABASE_URL,
        "dev_terminal_access": True,
        "allowed_origins": frozenset({ORIGIN}),
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
async def server() -> AsyncIterator[Server]:
    runtime = FakeRuntime()
    async with serve(create_app(settings(), lab_runtime=runtime)) as url:
        yield Server(url, runtime)


def lab_id(info: ContainerInfo) -> str:
    return info.labels["linuxlab.lab_id"]


async def wait_for(condition: object, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not callable(condition) or not condition():
            await asyncio.sleep(0.02)


# Handshake and access


async def test_terminal_route_is_off_by_default() -> None:
    runtime = FakeRuntime()
    app = create_app(settings(dev_terminal_access=False), lab_runtime=runtime)
    async with serve(app) as url:
        with pytest.raises(InvalidStatus):
            async with terminal(url, uuid.uuid4().hex):
                pass


@pytest.mark.parametrize("origin", ["http://evil.example", None])
async def test_rejects_handshake_from_other_origins(server: Server, origin: str | None) -> None:
    info = await server.lab()

    with pytest.raises(InvalidStatus) as error:
        async with terminal(server.url, lab_id(info), origin=origin):
            pass
    assert error.value.response.status_code == 403
    assert server.runtime.terminals == []


@pytest.mark.parametrize("path_id", ["0" * 32, "not-a-lab-id", "ll-lab-x", "fake-1"])
async def test_unknown_or_malformed_lab_ids_are_unavailable(server: Server, path_id: str) -> None:
    await server.lab()

    async with terminal(server.url, path_id) as websocket:
        assert await close_code(websocket) == 4404
    assert server.runtime.terminals == []


async def test_stopped_lab_is_unavailable(server: Server) -> None:
    info = await server.lab(running=False)

    async with terminal(server.url, lab_id(info)) as websocket:
        assert await close_code(websocket) == 4404


async def test_terminal_opens_only_on_the_requested_lab(server: Server) -> None:
    lab_a = await server.lab()
    lab_b = await server.lab()

    async with terminal(server.url, lab_id(lab_b)) as websocket:
        await start(websocket)
        assert server.last_terminal().container_id == lab_b.id
    assert all(t.container_id != lab_a.id for t in server.runtime.terminals)


# Init


@pytest.mark.parametrize(
    "first",
    [
        b"ls\r",
        json.dumps({"type": "resize", "cols": 80, "rows": 24}),
        json.dumps({"type": "init", "cols": 9999, "rows": 24}),
        "garbage",
    ],
)
async def test_first_message_must_be_a_valid_init(server: Server, first: bytes | str) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await websocket.send(first)
        assert await close_code(websocket) == 1008
    assert server.runtime.terminals == []


async def test_init_must_arrive_in_time(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("linuxlab.labs.terminal.router.INIT_TIMEOUT_SECONDS", 0.2)
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        assert await close_code(websocket) == 1008


async def test_init_sets_the_terminal_size(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket, cols=132, rows=40)
        assert server.last_terminal().size == TerminalSize(132, 40)


# Data and control


async def test_input_bytes_reach_the_terminal_and_output_returns(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        await websocket.send(b"echo \x1b[A\x7f\x03\x04\r")

        assert await read_until(websocket, b"\r") == b"echo \x1b[A\x7f\x03\x04\r"
        assert bytes(server.last_terminal().received) == b"echo \x1b[A\x7f\x03\x04\r"


async def test_output_is_forwarded_as_it_arrives(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        server.last_terminal().emit(b"partial")
        assert await read_until(websocket, b"partial") == b"partial"


async def test_large_output_is_split_into_frames(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        server.last_terminal().emit(b"x" * (MAX_FRAME_BYTES * 2 + 1))

        frames: list[bytes | str] = []
        async with asyncio.timeout(10):
            while sum(len(frame) for frame in frames) < MAX_FRAME_BYTES * 2 + 1:
                frames.append(await websocket.recv())
    assert [len(frame) for frame in frames] == [MAX_FRAME_BYTES, MAX_FRAME_BYTES, 1]


async def test_resize_is_applied(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        await websocket.send(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        await websocket.send(b"sync")
        await read_until(websocket, b"sync")

        assert server.last_terminal().resizes == [TerminalSize(100, 30)]


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (json.dumps({"type": "resize", "cols": 100000, "rows": 30}), 1008),
        (json.dumps({"type": "init", "cols": 80, "rows": 24}), 1008),
        (json.dumps({"type": "resize", "cols": 80, "rows": 24, "user": "root"}), 1008),
        (b"x" * (MAX_FRAME_BYTES + 1), 1009),
    ],
    ids=["resize-out-of-range", "second-init", "resize-extra-field", "input-too-large"],
)
async def test_invalid_messages_close_the_connection_and_the_terminal(
    server: Server, message: bytes | str, code: int
) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        await websocket.send(message)
        assert await close_code(websocket) == code
    assert server.last_terminal().closed
    assert server.last_terminal().resizes == []


# Endings


async def test_shell_exit_is_reported(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        server.last_terminal().exit(3)

        assert await next_control(websocket) == {"type": "exit", "code": 3}
        assert await close_code(websocket) == 4000
    assert server.last_terminal().closed


async def test_runtime_failure_is_reported(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        server.last_terminal().fail(LabRuntimeError("docker went away"))

        message = await next_control(websocket)
        assert message["type"] == "error"
        assert "docker" not in message["message"]
        assert await close_code(websocket) == 1011
    assert server.last_terminal().closed


async def test_client_disconnect_closes_the_terminal(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        session = server.last_terminal()
    await wait_for(lambda: session.closed)

    await wait_for(lambda: not _terminal_tasks())


async def test_abrupt_disconnect_closes_the_terminal(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        session = server.last_terminal()
        websocket.transport.abort()
    await wait_for(lambda: session.closed)

    await wait_for(lambda: not _terminal_tasks())


async def test_second_connection_replaces_the_first(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as first:
        await start(first)
        first_session = server.last_terminal()
        async with terminal(server.url, lab_id(info)) as second:
            await start(second)
            assert await close_code(first) == 4409
            await wait_for(lambda: first_session.closed)

            await second.send(b"still here")
            assert await read_until(second, b"still here") == b"still here"
            assert not server.last_terminal().closed


async def test_output_after_client_left_does_not_break_the_server(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        session = server.last_terminal()
    session.emit(b"late output")
    await wait_for(lambda: session.closed)

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await websocket.recv()


def _terminal_tasks() -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("terminal-")]


async def test_connection_closed_is_not_left_half_open(server: Server) -> None:
    info = await server.lab()

    async with terminal(server.url, lab_id(info)) as websocket:
        await start(websocket)
        server.last_terminal().exit(0)
        await next_control(websocket)
        with pytest.raises(ConnectionClosed):
            async with asyncio.timeout(5):
                while True:
                    await websocket.recv()
