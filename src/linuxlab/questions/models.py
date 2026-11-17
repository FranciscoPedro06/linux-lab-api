"""user_questions and question_attempts: a user's answers to questions.

- question_attempts holds every answer a user submitted that the API checked: the
  question, the version it was checked against, the option chosen and whether it was
  correct. Rows are final when inserted (trigger in the migration); a new answer adds
  a row and never changes an earlier one. DELETE stays possible so that deleting a
  user cascades to their attempts.
- user_questions holds the first correct answer of a user to a question, like
  user_missions for missions: inserted by that answer, in its transaction, and never
  changed afterwards. Completion belongs to the question, not to a version: a newer
  version does not undo it.
- Both reference the version through (question_id, version), so a row can only name a
  version of its own question.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from linuxlab.content.schema import OPTION_ID
from linuxlab.db import Base


class UserQuestion(Base):
    __tablename__ = "user_questions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["question_id", "completed_version"],
            ["question_versions.question_id", "question_versions.version"],
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    question_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The version of the first correct answer, and when it was given.
    completed_version: Mapped[int] = mapped_column(Integer)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class QuestionAttempt(Base):
    __tablename__ = "question_attempts"
    __table_args__ = (
        CheckConstraint(f"option ~ '^{OPTION_ID.pattern}$'", name="option_format"),
        ForeignKeyConstraint(
            ["question_id", "question_version"],
            ["question_versions.question_id", "question_versions.version"],
        ),
        Index(
            "ix_question_attempts_user_id_question_id_question_version",
            "user_id",
            "question_id",
            "question_version",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    question_id: Mapped[int] = mapped_column(BigInteger)
    # The question's current version when the answer was checked.
    question_version: Mapped[int] = mapped_column(Integer)
    # The id of the option chosen, one of that version's options.
    option: Mapped[str] = mapped_column(Text)
    correct: Mapped[bool] = mapped_column(Boolean)
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
