"""validation_runs: one row per finished validation of a lab.

- A run references its lab together with the lab's pinned mission version, through
  a composite foreign key to (lab_sessions.id, mission_id, mission_version): a run
  can only record the version its lab was created for, never the mission's current
  one. It also references that version in mission_versions.
- Rows are final when inserted and never updated (trigger in the migration). There
  is no `running` state; a validation that does not finish leaves no row.
- `result` holds the tree of statuses and closed reasons only. Expected values,
  observed values, file contents, command lines, the answer and the parameters are
  never stored.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from linuxlab.db import Base


class Outcome(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class ErrorCode(enum.StrEnum):
    """Why a run ended in `error`."""

    INVALID_SPEC = "invalid_spec"  # the pinned version or the lab's parameters cannot be used
    PROBE_TIMEOUT = "probe_timeout"
    PROBE_FAILED = "probe_failed"  # labctl probe exited non-zero, or the exec failed
    INVALID_PROBE_OUTPUT = "invalid_probe_output"
    CONTAINER_GONE = "container_gone"  # the container stopped or disappeared mid-validation
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    CHECK_FAILED = "check_failed"  # the probe answered, but a fact could not be read


def _sql_list(values: type[enum.StrEnum]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(str(value) for value in values))


class ValidationRun(Base):
    __tablename__ = "validation_runs"
    __table_args__ = (
        CheckConstraint(f"outcome IN ({_sql_list(Outcome)})", name="outcome"),
        CheckConstraint(f"error_code IN ({_sql_list(ErrorCode)})", name="error_code"),
        # An error code exactly when the run ended in error.
        CheckConstraint(
            "(outcome = 'error') = (error_code IS NOT NULL)", name="error_code_when_error"
        ),
        # Only a version that cannot be read leaves no result tree.
        CheckConstraint(
            "(result IS NULL) = (error_code IS NOT NULL AND error_code = 'invalid_spec')",
            name="result_present",
        ),
        CheckConstraint("result IS NULL OR jsonb_typeof(result) = 'object'", name="result_object"),
        CheckConstraint("finished_at >= started_at", name="finished_after_start"),
        # The run's version is its lab's pinned version.
        ForeignKeyConstraint(
            ["lab_session_id", "mission_id", "mission_version"],
            ["lab_sessions.id", "lab_sessions.mission_id", "lab_sessions.mission_version"],
            name="fk_validation_runs_lab_session_id_lab_sessions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["mission_id", "mission_version"],
            ["mission_versions.mission_id", "mission_versions.version"],
            name="fk_validation_runs_mission_id_mission_versions",
        ),
        Index("ix_validation_runs_lab_session_id_started_at", "lab_session_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    lab_session_id: Mapped[uuid.UUID] = mapped_column()
    mission_id: Mapped[int] = mapped_column(BigInteger)
    mission_version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    # Statuses and closed reasons per node (linuxlab.validation.evaluate.to_record).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Whether an answer was submitted. The answer itself is never stored.
    answer_provided: Mapped[bool] = mapped_column(Boolean)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
