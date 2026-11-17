"""Recording answers and reading a user's progress on questions.

An answer is checked by the API against the question's current version and recorded
as an attempt, whatever its result. The first correct answer also records the
completion, in the same transaction: the insert does nothing when the user already
completed the question, so the first completion is kept and two answers racing to
record it leave one row (as linuxlab.progress.service does for missions).

Progress, as the client sees it:

- `completed` once a correct answer was recorded, on any version: a newer version does
  not undo it, as for missions;
- otherwise `attempted` when the user answered the current version, and
  `not_answered` when they did not. Answers to an earlier version are kept in the
  history but count for that version only: `attempts` is the number of answers to the
  current version.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from linuxlab.questions.models import QuestionAttempt, UserQuestion

QuestionProgressStatus = Literal["not_answered", "attempted", "completed"]


class QuestionProgress(BaseModel):
    status: QuestionProgressStatus
    # Answers the user gave to the question's current version.
    attempts: int
    # The version of the first correct answer, and when it was given.
    completed_version: int | None
    completed_at: datetime | None

    @classmethod
    def of(
        cls, attempts: int, completed_version: int | None, completed_at: datetime | None
    ) -> "QuestionProgress":
        if completed_version is not None and completed_at is not None:
            status: QuestionProgressStatus = "completed"
        else:
            completed_version = completed_at = None
            status = "attempted" if attempts else "not_answered"
        return cls(
            status=status,
            attempts=attempts,
            completed_version=completed_version,
            completed_at=completed_at,
        )


def attempt_counts(user_id: uuid.UUID) -> Select[int, int, int]:
    """The user's number of answers per question and version, to join as a subquery."""
    return (
        select(
            QuestionAttempt.question_id,
            QuestionAttempt.question_version,
            func.count().label("attempts"),
        )
        .where(QuestionAttempt.user_id == user_id)
        .group_by(QuestionAttempt.question_id, QuestionAttempt.question_version)
    )


async def record_answer(
    db: AsyncSession,
    user_id: uuid.UUID,
    question_id: int,
    version: int,
    option: str,
    correct: bool,
) -> bool:
    """Record an answer, and the first completion when it is correct, in the caller's
    transaction. True if this answer recorded the completion."""
    answered_at = await db.scalar(
        insert(QuestionAttempt)
        .values(
            user_id=user_id,
            question_id=question_id,
            question_version=version,
            option=option,
            correct=correct,
        )
        .returning(QuestionAttempt.answered_at)
    )
    if not correct:
        return False
    inserted = await db.scalar(
        insert(UserQuestion)
        .values(
            user_id=user_id,
            question_id=question_id,
            completed_version=version,
            completed_at=answered_at,
        )
        .on_conflict_do_nothing(index_elements=[UserQuestion.user_id, UserQuestion.question_id])
        .returning(UserQuestion.user_id)
    )
    return inserted is not None


async def progress_of(
    db: AsyncSession, user_id: uuid.UUID, question_id: int, current_version: int
) -> QuestionProgress:
    attempts = await db.scalar(
        select(func.count()).where(
            (QuestionAttempt.user_id == user_id)
            & (QuestionAttempt.question_id == question_id)
            & (QuestionAttempt.question_version == current_version)
        )
    )
    completion = (
        await db.execute(
            select(UserQuestion.completed_version, UserQuestion.completed_at).where(
                (UserQuestion.user_id == user_id) & (UserQuestion.question_id == question_id)
            )
        )
    ).one_or_none()
    done_version, done_at = completion if completion else (None, None)
    return QuestionProgress.of(attempts or 0, done_version, done_at)
