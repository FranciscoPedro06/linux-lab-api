"""Moves bytes between a WebSocket and a terminal session."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from starlette.websockets import WebSocket, WebSocketDisconnect

from linuxlab.labs.runtime import TerminalSession
from linuxlab.labs.terminal.protocol import (
    MAX_FRAME_BYTES,
    OUTPUT_BYTES_PER_SECOND,
    CloseCode,
    ProtocolError,
    Resize,
    parse_control,
)


class Outcome(Enum):
    SHELL_EXITED = "shell_exited"
    CLIENT_CLOSED = "client_closed"
    REPLACED = "replaced"
    LAB_ENDED = "lab_ended"


@dataclass
class RelayStats:
    bytes_in: int = 0
    bytes_out: int = 0


@dataclass
class TerminalClaim:
    """A terminal connection's hold on its lab. `stop` is set when it must end."""

    stop: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: Outcome = Outcome.REPLACED
    released: asyncio.Event = field(default_factory=asyncio.Event)


class TerminalRegistry:
    """Terminal connections by lab, and when each lab's terminal was last used.

    At most one connection per lab: a new connection replaces the old one. Activity
    (connecting, disconnecting, input) is recorded in memory with no I/O, and taken
    by the reaper, which stores it with the lab session.
    """

    def __init__(self) -> None:
        self._active: dict[str, TerminalClaim] = {}
        self._activity: dict[str, datetime] = {}

    @asynccontextmanager
    async def claim(self, lab_key: str) -> AsyncIterator[TerminalClaim]:
        previous = self._active.get(lab_key)
        if previous is not None:
            previous.outcome = Outcome.REPLACED
            previous.stop.set()
        claim = TerminalClaim()
        self._active[lab_key] = claim
        self.touch(lab_key)
        try:
            yield claim
        finally:
            if self._active.get(lab_key) is claim:
                del self._active[lab_key]
            self.touch(lab_key)
            claim.released.set()

    async def end(self, lab_key: str) -> None:
        """Close the lab's terminal, if any, and wait until its cleanup has finished."""
        claim = self._active.get(lab_key)
        if claim is None:
            return
        claim.outcome = Outcome.LAB_ENDED
        claim.stop.set()
        await claim.released.wait()

    def connected(self, lab_key: str) -> bool:
        return lab_key in self._active

    def active(self) -> int:
        return len(self._active)

    def touch(self, lab_key: str) -> None:
        self._activity[lab_key] = datetime.now(UTC)

    def take_activity(self) -> dict[str, datetime]:
        """Activity recorded since the last call, by lab."""
        activity, self._activity = self._activity, {}
        return activity


class OutputPacer:
    """Token bucket that keeps output to the browser near a fixed rate.

    It slows a flood (`yes`, `cat /dev/urandom`) without dropping bytes: while the
    relay waits, it stops reading from the PTY, the PTY buffer fills, and the
    writing process blocks.
    """

    def __init__(self, bytes_per_second: int) -> None:
        self._rate = bytes_per_second
        self._tokens = float(bytes_per_second)
        self._updated = time.monotonic()

    async def consume(self, size: int) -> None:
        now = time.monotonic()
        self._tokens = min(self._rate, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        self._tokens -= size
        if self._tokens < 0:
            await asyncio.sleep(-self._tokens / self._rate)


async def relay(
    websocket: WebSocket,
    terminal: TerminalSession,
    claim: TerminalClaim,
    stats: RelayStats,
    on_input: Callable[[], None] = lambda: None,
) -> Outcome:
    """Run until the shell exits, the client leaves, or the claim is stopped (another
    connection took over or the lab ended).

    Raises ProtocolError for an invalid message and LabRuntimeError if the runtime
    fails. Both directions run as tasks; when one finishes, the others are cancelled
    and awaited, so nothing is left running.
    """

    async def output() -> Outcome:
        pacer = OutputPacer(OUTPUT_BYTES_PER_SECOND)
        while (chunk := await terminal.read()) is not None:
            for start in range(0, len(chunk), MAX_FRAME_BYTES):
                piece = chunk[start : start + MAX_FRAME_BYTES]
                try:
                    await websocket.send_bytes(piece)
                except (WebSocketDisconnect, OSError, RuntimeError):
                    return Outcome.CLIENT_CLOSED
                stats.bytes_out += len(piece)
                await pacer.consume(len(piece))
        return Outcome.SHELL_EXITED

    async def input() -> Outcome:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return Outcome.CLIENT_CLOSED
            data = message.get("bytes")
            text = message.get("text")
            if data is not None:
                if len(data) > MAX_FRAME_BYTES:
                    raise ProtocolError("input frame too large", CloseCode.MESSAGE_TOO_BIG)
                stats.bytes_in += len(data)
                on_input()
                await terminal.write(data)
            elif text is not None:
                control = parse_control(text)
                if not isinstance(control, Resize):
                    raise ProtocolError("init was already received")
                await terminal.resize(control.size)

    async def stopped() -> Outcome:
        await claim.stop.wait()
        return claim.outcome

    tasks = [
        asyncio.create_task(output(), name="terminal-output"),
        asyncio.create_task(input(), name="terminal-input"),
        asyncio.create_task(stopped(), name="terminal-stopped"),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    finished = [task for task in tasks if task.done() and not task.cancelled()]
    # Several may finish together, e.g. the shell exits as the client leaves.
    # Errors win over outcomes so they are reported, then the order above.
    for task in finished:
        error = task.exception()
        if error is not None:
            raise error
    return finished[0].result()
