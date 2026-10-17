"""Reading and recording a user's progress on a mission.

A completion is recorded by the validation that passes, in the transaction that
stores its run, with the user, mission and version of the validated lab. The insert
does nothing when the user already completed the mission, so the first completion
is kept and two transactions racing to record it leave one row: PostgreSQL makes
the second wait for the first and skip the insert if the first commits.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from linuxlab.progress.models import UserMission

ProgressStatus = Literal["completed", "not_completed"]


class Progress(BaseModel):
    """The user's progress on a mission, as the client sees it."""

    # completed when a completion is recorded, whatever the mission's current version.
    status: ProgressStatus
    # The version the mission was first completed on, and when.
    completed_version: int | None
    completed_at: datetime | None

    @classmethod
    def of(cls, completed_version: int | None, completed_at: datetime | None) -> "Progress":
        if completed_version is None or completed_at is None:
            return NOT_COMPLETED
        return cls(
            status="completed", completed_version=completed_version, completed_at=completed_at
        )


NOT_COMPLETED = Progress(status="not_completed", completed_version=None, completed_at=None)


async def record_completion(
    db: AsyncSession, user_id: uuid.UUID, mission_id: int, version: int, at: datetime
) -> bool:
    """Record the user's first completion of the mission, in the caller's transaction.

    True if this call recorded it; False if a completion was already recorded,
    including one committed meanwhile by another transaction.
    """
    inserted = await db.scalar(
        insert(UserMission)
        .values(user_id=user_id, mission_id=mission_id, completed_version=version, completed_at=at)
        .on_conflict_do_nothing(index_elements=[UserMission.user_id, UserMission.mission_id])
        .returning(UserMission.user_id)
    )
    return inserted is not None


async def progress_of(db: AsyncSession, user_id: uuid.UUID, mission_id: int) -> Progress:
    row = (
        await db.execute(
            select(UserMission.completed_version, UserMission.completed_at).where(
                (UserMission.user_id == user_id) & (UserMission.mission_id == mission_id)
            )
        )
    ).one_or_none()
    return Progress.of(*row) if row else NOT_COMPLETED
