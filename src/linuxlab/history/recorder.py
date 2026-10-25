"""Hands the command lines observed by one terminal connection to the database.

The relay calls `observe` from its output task. It never waits and never fails: a
command is either queued for the connection's writer task or dropped, and dropping
is only counted. So the history can lose entries, but it never slows down, blocks or
ends a terminal. The best-effort rules:

- at most MAX_EVENTS_PER_SECOND per connection (a bucket of that size, refilled at
  that rate); more are dropped;
- at most QUEUE_SIZE waiting to be written; more are dropped;
- once the lab holds MAX_ENTRIES_PER_LAB entries, every later one is dropped;
- an entry whose insert fails (the database is unavailable, the lab is gone) is lost;
- when the connection ends, the writer has DRAIN_SECONDS to write what is queued;
  what is left is dropped and the writer is cancelled.

The recorder is bound to one lab when it is created, from the lab the connection was
authorized for, so a connection only ever writes to its own lab. Logs carry counts
and error types, never a command.
"""

import asyncio
import collections
import enum
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Protocol

from linuxlab.history.marker import Rejection
from linuxlab.history.service import LabHistoryFullError

logger = logging.getLogger(__name__)

MAX_EVENTS_PER_SECOND = 20
QUEUE_SIZE = 100
DRAIN_SECONDS = 2.0


class HistoryStore(Protocol):
    async def record(self, lab_id: uuid.UUID, command: str, at: datetime) -> int: ...


class Dropped(enum.StrEnum):
    RATE_LIMITED = "rate_limited"
    QUEUE_FULL = "queue_full"
    LAB_FULL = "lab_full"
    WRITE_FAILED = "write_failed"
    NOT_DRAINED = "not_drained"


class HistoryRecorder:
    def __init__(
        self,
        store: HistoryStore,
        lab_id: uuid.UUID,
        connection: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._store = store
        self._lab_id = lab_id
        self._connection = connection
        self._clock = clock
        self._drain_seconds = drain_seconds
        self._queue: asyncio.Queue[tuple[str, datetime]] = asyncio.Queue(QUEUE_SIZE)
        self._tokens = float(MAX_EVENTS_PER_SECOND)
        self._refilled = clock()
        self._lab_full = False
        self._open = False
        # Set while an insert runs, so a cancelled one is counted as not drained.
        self._writing = False
        self.recorded = 0
        self.dropped: collections.Counter[Dropped] = collections.Counter()
        self.rejected: collections.Counter[Rejection] = collections.Counter()

    def observe(self, command: str) -> None:
        """Queue a command line for writing, or drop it. Never waits."""
        if not self._open:
            return
        if self._lab_full:
            self.dropped[Dropped.LAB_FULL] += 1
            return
        now = self._clock()
        self._tokens = min(
            MAX_EVENTS_PER_SECOND, self._tokens + (now - self._refilled) * MAX_EVENTS_PER_SECOND
        )
        self._refilled = now
        if self._tokens < 1:
            self.dropped[Dropped.RATE_LIMITED] += 1
            return
        self._tokens -= 1
        try:
            self._queue.put_nowait((command, datetime.now(UTC)))
        except asyncio.QueueFull:
            self.dropped[Dropped.QUEUE_FULL] += 1

    def reject(self, rejection: Rejection) -> None:
        """Count a marker whose content was refused."""
        self.rejected[rejection] += 1

    @asynccontextmanager
    async def running(self) -> AsyncIterator["HistoryRecorder"]:
        """Accept commands and write them until the block ends, then drain."""
        self._open = True
        writer = asyncio.create_task(self._write(), name="terminal-history")
        try:
            yield self
        finally:
            self._open = False
            with suppress(TimeoutError):
                async with asyncio.timeout(self._drain_seconds):
                    await self._queue.join()
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer
            # Left in the queue, or being written when the writer was cancelled.
            not_drained = self._queue.qsize() + int(self._writing)
            if not_drained:
                self.dropped[Dropped.NOT_DRAINED] += not_drained
            self._log()

    async def _write(self) -> None:
        failure_logged = False
        while True:
            command, at = await self._queue.get()
            self._writing = True
            try:
                await self._store.record(self._lab_id, command, at)
                self.recorded += 1
            except LabHistoryFullError:
                self._lab_full = True
                self.dropped[Dropped.LAB_FULL] += 1
            except Exception as error:
                self.dropped[Dropped.WRITE_FAILED] += 1
                if not failure_logged:
                    failure_logged = True
                    logger.warning(
                        "terminal history write failed: connection=%s lab=%s error=%s",
                        self._connection,
                        self._lab_id,
                        type(error).__name__,
                    )
            self._writing = False
            self._queue.task_done()

    def _log(self) -> None:
        if not (self.recorded or self.dropped or self.rejected):
            return
        details = [f"{reason}={count}" for reason, count in sorted(self.dropped.items())]
        details += [f"rejected_{reason}={count}" for reason, count in sorted(self.rejected.items())]
        logger.info(
            "terminal history: connection=%s lab=%s recorded=%d%s",
            self._connection,
            self._lab_id,
            self.recorded,
            "".join(f" {detail}" for detail in details),
        )
