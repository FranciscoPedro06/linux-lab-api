"""Terminal WebSocket against PostgreSQL and a FakeRuntime: authentication, ownership,
lab state, protocol and connection lifecycle."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from linuxlab.labs.models import EndReason, LabSession
from linuxlab.labs.runtime import LabRuntimeError, TerminalSize
from linuxlab.labs.runtime.fake import FakeRuntime, FakeTerminalSession
from linuxlab.labs.runtime.spec import container_name
from linuxlab.labs.terminal.protocol import MAX_FRAME_BYTES

from .app_support import Account, Api, running_api
from .terminal_client import close_code, next_control, read_until, start, terminal

pytestmark = pytest.mark.integration


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


def runtime(api: Api) -> FakeRuntime:
    fake = api.app.state.runtime
    assert isinstance(fake, FakeRuntime)
    return fake


def last_terminal(api: Api) -> FakeTerminalSession:
    return runtime(api).terminals[-1]


def container_id(api: Api, lab: LabSession) -> str:
    name = container_name(lab.lab_key)
    return next(c.id for c in runtime(api).containers() if c.name == name)


async def wait_for(condition: object, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while not callable(condition) or not condition():
            await asyncio.sleep(0.02)


# Handshake: origin, session, ownership and lab state


@pytest.mark.parametrize("origin", ["http://evil.example", None])
async def test_rejects_handshake_from_other_origins(
    api: Api, ana: Account, lab: LabSession, origin: str | None
) -> None:
    with pytest.raises(InvalidStatus) as error:
        async with terminal(api.url, str(lab.id), ana.token, origin=origin):
            pass
    assert error.value.response.status_code == 403
    assert runtime(api).terminals == []


@pytest.mark.parametrize("token", [None, "", "not-a-session", "x" * 500])
async def test_requires_a_session(api: Api, lab: LabSession, token: str | None) -> None:
    async with terminal(api.url, str(lab.id), token) as websocket:
        assert await close_code(websocket) == 4401
    assert runtime(api).terminals == []


async def test_expired_session_is_refused(api: Api, ana: Account, lab: LabSession) -> None:
    await api.sql("UPDATE auth_sessions SET last_seen_at = now() - interval '8 days'")

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        assert await close_code(websocket) == 4401


async def test_session_of_another_user_cannot_open_the_lab(
    api: Api, ana: Account, lab: LabSession
) -> None:
    bia = await api.account("bia@example.com")
    lab_b = await api.lab(bia)

    async with terminal(api.url, str(lab.id), bia.token) as websocket:
        assert await close_code(websocket) == 4404
    assert runtime(api).terminals == []

    # Each user still opens their own.
    async with terminal(api.url, str(lab_b.id), bia.token) as websocket:
        await start(websocket)
        assert last_terminal(api).container_id == container_id(api, lab_b)


@pytest.mark.parametrize(
    "path_id", [str(uuid.uuid4()), "not-a-lab-id", "0" * 32, "ll-lab-x", "fake-1"]
)
async def test_unknown_or_malformed_lab_ids_are_unavailable(
    api: Api, ana: Account, lab: LabSession, path_id: str
) -> None:
    async with terminal(api.url, path_id, ana.token) as websocket:
        assert await close_code(websocket) == 4404
    assert runtime(api).terminals == []


async def test_container_name_or_hex_id_is_not_a_lab_id(
    api: Api, ana: Account, lab: LabSession
) -> None:
    for path_id in (lab.lab_key, container_name(lab.lab_key)):
        async with terminal(api.url, path_id, ana.token) as websocket:
            assert await close_code(websocket) == 4404


async def test_ended_lab_is_gone(api: Api, ana: Account, lab: LabSession) -> None:
    await api.end(lab)

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        assert await close_code(websocket) == 4410
    assert runtime(api).terminals == []


@pytest.mark.parametrize(("oom", "reason"), [(True, "oom"), (False, "container_lost")])
async def test_lab_whose_container_died_is_gone_and_ended(
    api: Api, ana: Account, lab: LabSession, oom: bool, reason: str
) -> None:
    runtime(api).crash(container_id(api, lab), oom=oom)

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        assert await close_code(websocket) == 4410
    assert await api.status(lab) == ("terminated", reason)


async def test_terminal_opens_on_the_owned_lab(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        assert last_terminal(api).container_id == container_id(api, lab)


# The lab ending while connected


async def test_ending_the_lab_closes_the_terminal_before_removing_the_container(
    api: Api, ana: Account, lab: LabSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = runtime(api)
    remove = fake.remove
    closed_when_removed: list[bool] = []

    async def recording_remove(container_id: str) -> None:
        closed_when_removed.append(all(t.closed for t in fake.terminals))
        await remove(container_id)

    monkeypatch.setattr(fake, "remove", recording_remove)
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await api.end(lab)

        assert await close_code(websocket) == 4410
    assert closed_when_removed == [True]
    assert await api.status(lab) == ("terminated", "user")
    assert fake.containers() == []


@pytest.mark.parametrize(("oom", "reason"), [(True, "oom"), (False, "container_lost")])
async def test_container_dying_during_the_session_ends_the_lab(
    api: Api, ana: Account, lab: LabSession, oom: bool, reason: str
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        runtime(api).crash(container_id(api, lab), oom=oom)

        assert await close_code(websocket) == 4410
    assert await api.status(lab) == ("terminated", reason)


async def test_lab_ending_before_the_terminal_starts(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        # The handshake was accepted; the lab ends before init arrives.
        await asyncio.sleep(0.1)
        await api.end(lab)
        await websocket.send(json.dumps({"type": "init", "cols": 80, "rows": 24}))

        assert await close_code(websocket) == 4410
    assert runtime(api).terminals == []


async def test_ending_one_lab_leaves_another_users_terminal_open(
    api: Api, ana: Account, lab: LabSession
) -> None:
    bia = await api.account("bia@example.com")
    lab_b = await api.lab(bia)

    async with terminal(api.url, str(lab_b.id), bia.token) as websocket_b:
        await start(websocket_b)
        session_b = last_terminal(api)
        async with terminal(api.url, str(lab.id), ana.token) as websocket_a:
            await start(websocket_a)
            await api.end(lab)
            assert await close_code(websocket_a) == 4410

        await websocket_b.send(b"still here")
        assert await read_until(websocket_b, b"still here") == b"still here"
        assert not session_b.closed


async def test_reconnecting_after_the_lab_ended_is_refused(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await api.end(lab)
        assert await close_code(websocket) == 4410

    for _ in range(3):
        async with terminal(api.url, str(lab.id), ana.token) as websocket:
            assert await close_code(websocket) == 4410


# Activity


async def test_connection_and_input_are_recorded_as_activity(
    api: Api, ana: Account, lab: LabSession
) -> None:
    api.terminals.take_activity()

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        assert lab.lab_key in api.terminals.take_activity()
        await websocket.send(b"x")
        await read_until(websocket, b"x")
        assert lab.lab_key in api.terminals.take_activity()
        assert api.terminals.connected(lab.lab_key)

    await wait_for(lambda: not api.terminals.connected(lab.lab_key))
    assert lab.lab_key in api.terminals.take_activity()


async def test_output_alone_is_not_activity(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        api.terminals.take_activity()
        last_terminal(api).emit(b"running process output")
        await read_until(websocket, b"output")

        assert api.terminals.take_activity() == {}


async def test_idle_connected_terminal_is_ended_by_the_reaper(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        api.terminals.take_activity()
        await api.sql(
            "UPDATE lab_sessions SET last_activity_at = now() - CAST(:idle AS interval)",
            idle=timedelta(minutes=31),
        )
        await api.labs.reconcile()

        assert await close_code(websocket) == 4410
    assert await api.status(lab) == ("terminated", "no_input")


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
async def test_first_message_must_be_a_valid_init(
    api: Api, ana: Account, lab: LabSession, first: bytes | str
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await websocket.send(first)
        assert await close_code(websocket) == 1008
    assert runtime(api).terminals == []


async def test_init_must_arrive_in_time(
    api: Api, ana: Account, lab: LabSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("linuxlab.labs.terminal.router.INIT_TIMEOUT_SECONDS", 0.2)

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        assert await close_code(websocket) == 1008


async def test_init_sets_the_terminal_size(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket, cols=132, rows=40)
        assert last_terminal(api).size == TerminalSize(132, 40)


# Data and control


async def test_input_bytes_reach_the_terminal_and_output_returns(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(b"echo \x1b[A\x7f\x03\x04\r")

        assert await read_until(websocket, b"\r") == b"echo \x1b[A\x7f\x03\x04\r"
        assert bytes(last_terminal(api).received) == b"echo \x1b[A\x7f\x03\x04\r"


async def test_large_output_is_split_into_frames(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        last_terminal(api).emit(b"x" * (MAX_FRAME_BYTES * 2 + 1))

        frames: list[bytes | str] = []
        async with asyncio.timeout(10):
            while sum(len(frame) for frame in frames) < MAX_FRAME_BYTES * 2 + 1:
                frames.append(await websocket.recv())
    assert [len(frame) for frame in frames] == [MAX_FRAME_BYTES, MAX_FRAME_BYTES, 1]


async def test_resize_is_applied(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        await websocket.send(b"sync")
        await read_until(websocket, b"sync")

        assert last_terminal(api).resizes == [TerminalSize(100, 30)]


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
    api: Api, ana: Account, lab: LabSession, message: bytes | str, code: int
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await websocket.send(message)
        assert await close_code(websocket) == code
    assert last_terminal(api).closed
    assert last_terminal(api).resizes == []


# Endings


async def test_shell_exit_is_reported(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        last_terminal(api).exit(3)

        assert await next_control(websocket) == {"type": "exit", "code": 3}
        assert await close_code(websocket) == 4000
    assert last_terminal(api).closed
    # Only the shell ended; the lab is still ready.
    assert await api.status(lab) == ("ready", None)


async def test_runtime_failure_is_reported(api: Api, ana: Account, lab: LabSession) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        last_terminal(api).fail(LabRuntimeError("docker went away"))

        message = await next_control(websocket)
        assert message["type"] == "error"
        assert "docker" not in message["message"]
        assert await close_code(websocket) == 1011
    assert last_terminal(api).closed
    assert await api.status(lab) == ("ready", None)


async def test_client_disconnect_closes_the_terminal(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        session = last_terminal(api)
    await wait_for(lambda: session.closed)
    await wait_for(lambda: not _terminal_tasks())


async def test_abrupt_disconnect_closes_the_terminal(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        session = last_terminal(api)
        websocket.transport.abort()
    await wait_for(lambda: session.closed)
    await wait_for(lambda: not _terminal_tasks())


async def test_second_connection_replaces_the_first(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as first:
        await start(first)
        first_session = last_terminal(api)
        async with terminal(api.url, str(lab.id), ana.token) as second:
            await start(second)
            assert await close_code(first) == 4409
            await wait_for(lambda: first_session.closed)

            await second.send(b"still here")
            assert await read_until(second, b"still here") == b"still here"
            assert not last_terminal(api).closed


async def test_connection_closed_is_not_left_half_open(
    api: Api, ana: Account, lab: LabSession
) -> None:
    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        last_terminal(api).exit(0)
        await next_control(websocket)
        with pytest.raises(ConnectionClosed):
            async with asyncio.timeout(5):
                while True:
                    await websocket.recv()


def _terminal_tasks() -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task.get_name().startswith("terminal-")]


async def test_end_reason_is_kept_when_the_lab_ends_twice(
    api: Api, ana: Account, lab: LabSession
) -> None:
    await api.end(lab, EndReason.LOGOUT)
    await api.end(lab, EndReason.USER)

    assert await api.status(lab) == ("terminated", "logout")
