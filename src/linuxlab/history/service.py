"""Storing and reading a lab's command history.

Each command is inserted in its own short transaction, which also assigns its `seq`:
one more than the lab's highest. Writers of the same lab (a terminal connection being
replaced while the next one starts, or two API processes) are serialized by a
transaction-level advisory lock keyed by the lab, taken just before the insert and
released by its commit. So seq has no gaps and follows the order in which entries
were committed, and a reader paging with `after` never misses an entry committed
later with a smaller seq. Two labs whose keys collide only wait for each other.

No other lock is taken, nothing is held beyond that transaction, and nothing here
touches the lab's row or its container.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linuxlab.history.models import CommandHistoryEntry

MAX_ENTRIES_PER_LAB = 2000
# First key of the advisory locks taken here, so they cannot meet other uses of
# advisory locks. The second key is a hash of the lab id.
LOCK_NAMESPACE = 7771

_LOCK = text("SELECT pg_advisory_xact_lock(:namespace, hashtext(CAST(CAST(:lab AS uuid) AS text)))")
_INSERT = text(
    "INSERT INTO command_history (lab_session_id, seq, command, recorded_at)"
    " SELECT CAST(:lab AS uuid), COALESCE(MAX(seq), 0) + 1, CAST(:command AS text),"
    " CAST(:at AS timestamptz)"
    " FROM command_history WHERE lab_session_id = CAST(:lab AS uuid)"
    " HAVING COALESCE(MAX(seq), 0) < :limit"
    " RETURNING seq"
)


class LabHistoryFullError(Exception):
    """The lab already has MAX_ENTRIES_PER_LAB entries."""


@dataclass(frozen=True)
class Page:
    entries: list[CommandHistoryEntry]
    # Whether entries exist after the last one returned.
    more: bool


class CommandHistory:
    """Writes entries observed by terminal connections."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def record(self, lab_id: uuid.UUID, command: str, at: datetime) -> int:
        """Append the command to the lab's history and return its seq.

        Raises LabHistoryFullError when the lab is at its limit, and SQLAlchemyError if
        the database fails (including a lab that no longer exists).
        """
        async with self._sessionmaker() as db:
            await db.execute(_LOCK, {"namespace": LOCK_NAMESPACE, "lab": lab_id})
            seq = await db.scalar(
                _INSERT,
                {"lab": lab_id, "command": command, "at": at, "limit": MAX_ENTRIES_PER_LAB},
            )
            await db.commit()
        if seq is None:
            raise LabHistoryFullError(lab_id)
        return int(seq)


async def read_page(db: AsyncSession, lab_id: uuid.UUID, after: int, limit: int) -> Page:
    """Entries of the lab with seq greater than `after`, in seq order, at most `limit`."""
    rows = list(
        await db.scalars(
            select(CommandHistoryEntry)
            .where(CommandHistoryEntry.lab_session_id == lab_id, CommandHistoryEntry.seq > after)
            .order_by(CommandHistoryEntry.seq)
            .limit(limit + 1)
        )
    )
    return Page(entries=rows[:limit], more=len(rows) > limit)
