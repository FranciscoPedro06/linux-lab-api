"""HistoryRecorder: queueing, limits, failures and draining, without a database."""

import asyncio
import logging
import uuid
from datetime import datetime

import pytest

from linuxlab.history import recorder as recorder_module
from linuxlab.history.marker import Rejection
from linuxlab.history.recorder import (
    MAX_EVENTS_PER_SECOND,
    QUEUE_SIZE,
    Dropped,
    HistoryRecorder,
)
from linuxlab.history.service import LabHistoryFullError

LAB = uuid.uuid4()
SECRET = "mysql -psecret-value"


class Store:
    """Records what it is given. `gate`, when set, makes each insert wait for it."""

    def __init__(self) -> None:
        self.written: list[tuple[uuid.UUID, str]] = []
        self.gate: asyncio.Event | None = None
        self.started = 0
        self.fail_with: Exception | None = None

    async def record(self, lab_id: uuid.UUID, command: str, at: datetime) -> int:
        self.started += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        self.written.append((lab_id, command))
        return len(self.written)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(store: Store, clock: Clock | None = None, drain: float = 2.0) -> HistoryRecorder:
    return HistoryRecorder(store, LAB, "c0nnect10n", clock=clock or Clock(), drain_seconds=drain)


async def settle(store: Store, count: int) -> None:
    async with asyncio.timeout(5):
        while store.started < count:
            await asyncio.sleep(0.01)


async def test_commands_are_written_in_order_to_the_bound_lab() -> None:
    store = Store()
    recorder = make(store)
    async with recorder.running():
        for command in ("ls", "pwd", "ls"):
            recorder.observe(command)

    assert store.written == [(LAB, "ls"), (LAB, "pwd"), (LAB, "ls")]
    assert recorder.recorded == 3
    assert not recorder.dropped


async def test_nothing_is_accepted_outside_the_running_block() -> None:
    store = Store()
    recorder = make(store)
    recorder.observe("before")
    async with recorder.running():
        pass
    recorder.observe("after")

    assert store.written == []
    assert recorder.recorded == 0


async def test_more_than_the_rate_is_dropped_and_the_bucket_refills() -> None:
    store = Store()
    clock = Clock()
    recorder = make(store, clock)
    async with recorder.running():
        for index in range(MAX_EVENTS_PER_SECOND + 5):
            recorder.observe(f"echo {index}")
        clock.now += 0.5
        for index in range(MAX_EVENTS_PER_SECOND):
            recorder.observe(f"later {index}")

    assert MAX_EVENTS_PER_SECOND == 20
    assert recorder.recorded == MAX_EVENTS_PER_SECOND + MAX_EVENTS_PER_SECOND // 2
    assert recorder.dropped == {Dropped.RATE_LIMITED: 5 + MAX_EVENTS_PER_SECOND // 2}


async def test_a_full_queue_drops_new_commands_without_waiting() -> None:
    store = Store()
    store.gate = asyncio.Event()
    clock = Clock()
    recorder = make(store, clock)
    async with recorder.running():
        recorder.observe("first")
        await settle(store, 1)  # being written, no longer queued
        for index in range(QUEUE_SIZE + 3):
            clock.now += 1  # never rate limited
            recorder.observe(f"echo {index}")
        assert recorder.dropped == {Dropped.QUEUE_FULL: 3}
        store.gate.set()

    assert QUEUE_SIZE == 100
    assert recorder.recorded == 1 + QUEUE_SIZE


async def test_a_full_lab_stops_writing() -> None:
    store = Store()
    store.fail_with = LabHistoryFullError(LAB)
    clock = Clock()
    recorder = make(store, clock)
    async with recorder.running():
        recorder.observe("one")
        await settle(store, 1)
        await asyncio.sleep(0.05)
        for command in ("two", "three"):
            clock.now += 1
            recorder.observe(command)

    assert store.started == 1
    assert recorder.dropped == {Dropped.LAB_FULL: 3}


async def test_a_failed_write_loses_only_that_command_and_logs_no_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store()
    store.fail_with = RuntimeError(f"connection refused while writing {SECRET}")
    clock = Clock()
    recorder = make(store, clock)
    with caplog.at_level(logging.INFO, logger="linuxlab"):
        async with recorder.running():
            recorder.observe(SECRET)
            await settle(store, 1)
            await asyncio.sleep(0.05)
            store.fail_with = None
            clock.now += 1
            recorder.observe("ls")

    assert store.written == [(LAB, "ls")]
    assert recorder.dropped == {Dropped.WRITE_FAILED: 1}
    text = caplog.text
    assert "error=RuntimeError" in text
    assert "recorded=1 write_failed=1" in text
    assert "secret" not in text
    assert "connection refused" not in text


async def test_draining_is_bounded_and_the_writer_does_not_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store()
    store.gate = asyncio.Event()  # never set: the first insert never finishes
    clock = Clock()
    recorder = make(store, clock, drain=0.2)
    with caplog.at_level(logging.INFO, logger="linuxlab"):
        async with asyncio.timeout(2):
            async with recorder.running():
                for command in ("a", "b", "c"):
                    clock.now += 1
                    recorder.observe(command)
                await settle(store, 1)

    assert recorder.recorded == 0
    assert recorder.dropped == {Dropped.NOT_DRAINED: 3}
    assert not [task for task in asyncio.all_tasks() if task.get_name() == "terminal-history"]
    assert "not_drained=3" in caplog.text


async def test_queued_commands_are_written_before_the_block_ends() -> None:
    store = Store()
    store.gate = asyncio.Event()
    clock = Clock()
    recorder = make(store, clock)

    async def release() -> None:
        await asyncio.sleep(0.1)
        assert store.gate is not None
        store.gate.set()

    async with recorder.running():
        for command in ("a", "b"):
            clock.now += 1
            recorder.observe(command)
        asyncio.get_running_loop().create_task(release())

    assert [command for _, command in store.written] == ["a", "b"]
    assert not recorder.dropped


async def test_rejections_are_counted_and_logged_by_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = make(Store())
    with caplog.at_level(logging.INFO, logger="linuxlab"):
        async with recorder.running():
            recorder.reject(Rejection.TOO_LARGE)
            recorder.reject(Rejection.TOO_LARGE)
            recorder.reject(Rejection.INVALID_UTF8)

    assert "rejected_invalid_utf8=1 rejected_too_large=2" in caplog.text


async def test_a_connection_without_history_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="linuxlab"):
        async with make(Store()).running():
            pass
    assert "terminal history" not in caplog.text


def test_limits() -> None:
    assert recorder_module.DRAIN_SECONDS <= 2
