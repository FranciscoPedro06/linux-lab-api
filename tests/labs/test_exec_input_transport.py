"""How DockerRuntime writes an exec's standard input, on stand-in connections.

No Docker: the exec and its connection are replaced by objects that record, in
order, the half-close check, every write and the end of file. The real connection
is covered by test_docker_exec_input.py.
"""

import asyncio
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from aiodocker.stream import Message

from linuxlab.labs.runtime import LabRuntimeError
from linuxlab.labs.runtime.docker import STDIN_CHUNK_BYTES, _run

SCRIPT = b"echo setup\n" * 10_000  # larger than one chunk


class Transport:
    def __init__(self, events: list[Any], *, half_close: bool) -> None:
        self.events = events
        self.half_close = half_close
        self.ended = asyncio.Event()

    def is_closing(self) -> bool:
        return False

    def can_write_eof(self) -> bool:
        self.events.append("check")
        return self.half_close

    def write_eof(self) -> None:
        self.events.append("eof")
        self.ended.set()


class Stream:
    """Output arrives only after the end of file, like bash reading a script."""

    def __init__(self, transport: Transport, events: list[Any]) -> None:
        self._resp = SimpleNamespace(connection=SimpleNamespace(transport=transport))
        self.transport = transport
        self.events = events
        self.output = [Message(1, b"done\n")]

    async def write_in(self, data: bytes) -> None:
        self.events.append(("write", len(data)))

    async def read_out(self) -> Message | None:
        await self.transport.ended.wait()
        return self.output.pop(0) if self.output else None

    async def __aenter__(self) -> "Stream":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.events.append("closed")


class Execution:
    def __init__(self, stream: Stream) -> None:
        self.stream = stream

    def start(self, detach: bool) -> Stream:
        return self.stream


def execution(*, half_close: bool) -> tuple[Any, list[Any]]:
    events: list[Any] = []
    stream = Stream(Transport(events, half_close=half_close), events)
    return cast(Any, Execution(stream)), events


async def test_nothing_is_written_when_the_connection_cannot_half_close() -> None:
    exec_, events = execution(half_close=False)

    with pytest.raises(LabRuntimeError, match="cannot close standard input"):
        await asyncio.wait_for(_run(exec_, SCRIPT), timeout=5)

    # The check came first, and no byte of the script was sent before the
    # connection closed.
    assert events == ["check", "closed"]


async def test_supported_connection_writes_everything_then_the_end_of_file() -> None:
    exec_, events = execution(half_close=True)

    stdout, stderr, truncated = await asyncio.wait_for(_run(exec_, SCRIPT), timeout=5)

    assert (stdout, stderr, truncated) == (b"done\n", b"", False)
    writes = [event for event in events if isinstance(event, tuple)]
    assert sum(size for _, size in writes) == len(SCRIPT)
    assert all(size <= STDIN_CHUNK_BYTES for _, size in writes)
    assert events[0] == "check"
    assert events[-2:] == ["eof", "closed"]


async def test_without_stdin_nothing_is_checked_or_written() -> None:
    exec_, events = execution(half_close=False)
    cast(Any, exec_).stream.transport.ended.set()

    assert await asyncio.wait_for(_run(exec_, None), timeout=5) == (b"done\n", b"", False)
    assert events == ["closed"]
