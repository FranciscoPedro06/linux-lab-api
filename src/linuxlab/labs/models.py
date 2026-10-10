"""Lab sessions: which user owns which lab, and where it is in its lifecycle.

    provisioning --> ready --> terminating --> terminated
         |  '------------------^                   ^
         '----------> failed ----------------------'

- provisioning: the row exists; the container is being created and started.
- ready: the container is running and the terminal may be used.
- terminating: the lab has ended (end_reason says why); its terminal and
  container are being removed.
- failed: provisioning did not complete; the container may still exist.
- terminated: the container has been removed. Final.

provisioning -> terminating covers a lab ended (logout, delete) before it was ready.
The database refuses any other transition, and refuses changing the owner or a
recorded end reason (trigger in the migration that creates the table).

A lab is created for one version of a mission, with the parameters generated for it.
Both are fixed at creation: the trigger also refuses changing the mission, the
version or the parameters, and the version itself is immutable. Labs created before
missions existed have none of the three.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from linuxlab.db import Base


class LabStatus(enum.StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


# A user has at most one lab in these states.
ACTIVE_STATUSES = frozenset({LabStatus.PROVISIONING, LabStatus.READY, LabStatus.TERMINATING})
# States in which the lab has not ended yet.
RUNNING_STATUSES = frozenset({LabStatus.PROVISIONING, LabStatus.READY})

TRANSITIONS: dict[LabStatus, frozenset[LabStatus]] = {
    LabStatus.PROVISIONING: frozenset({LabStatus.READY, LabStatus.FAILED, LabStatus.TERMINATING}),
    LabStatus.READY: frozenset({LabStatus.TERMINATING}),
    LabStatus.TERMINATING: frozenset({LabStatus.TERMINATED}),
    LabStatus.FAILED: frozenset({LabStatus.TERMINATED}),
    LabStatus.TERMINATED: frozenset(),
}


class EndReason(enum.StrEnum):
    USER = "user"  # DELETE /api/labs/{id}
    LOGOUT = "logout"
    NO_TERMINAL = "no_terminal"  # no terminal connected for 15 minutes
    NO_INPUT = "no_input"  # terminal connected, no input for 30 minutes
    MAX_LIFETIME = "max_lifetime"  # 2 hours since creation
    OOM = "oom"  # the container was killed for exceeding its memory limit
    CONTAINER_LOST = "container_lost"  # the container stopped or disappeared otherwise
    PROVISIONING_FAILED = "provisioning_failed"
    PROVISIONING_TIMEOUT = "provisioning_timeout"


def _sql_list(values: frozenset[LabStatus] | type[enum.StrEnum]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(str(value) for value in values))


ACTIVE_STATUSES_SQL = _sql_list(ACTIVE_STATUSES)


class LabSession(Base):
    __tablename__ = "lab_sessions"
    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(LabStatus)})", name="status"),
        CheckConstraint(f"end_reason IN ({_sql_list(EndReason)})", name="end_reason"),
        # A lab has an end reason and an end time exactly when it has ended.
        CheckConstraint(
            f"(status IN ({_sql_list(RUNNING_STATUSES)})) = (end_reason IS NULL)",
            name="end_reason_when_ended",
        ),
        CheckConstraint(
            f"(status IN ({_sql_list(RUNNING_STATUSES)})) = (ended_at IS NULL)",
            name="ended_at_when_ended",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        # A lab has a mission, a version and parameters together, or none of them.
        CheckConstraint(
            "(mission_id IS NULL) = (mission_version IS NULL)"
            " AND (mission_id IS NULL) = (params IS NULL)",
            name="mission_pinned",
        ),
        CheckConstraint("params IS NULL OR jsonb_typeof(params) = 'object'", name="params_object"),
        # The version belongs to the mission: both columns reference the version's key.
        ForeignKeyConstraint(
            ["mission_id", "mission_version"],
            ["mission_versions.mission_id", "mission_versions.version"],
        ),
        Index(
            "one_active_lab_per_user",
            "user_id",
            unique=True,
            postgresql_where=text(f"status IN ({ACTIVE_STATUSES_SQL})"),
        ),
        Index("ix_lab_sessions_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text)
    end_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Last terminal connection, disconnection or input; the idle timeouts count from it.
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Maximum lifetime, fixed at creation.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When the lab left provisioning or ready.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The mission version pinned at creation. Provisioning, and everything after it,
    # reads this version, never the mission's current one.
    mission_id: Mapped[int | None] = mapped_column(BigInteger)
    mission_version: Mapped[int | None] = mapped_column(Integer)
    # Parameter name -> generated value, all strings. Secret: never sent to the client
    # or written to a log.
    params: Mapped[dict[str, str] | None] = mapped_column(JSONB)

    @property
    def lab_key(self) -> str:
        """The id as used by the runtime: container name and labels."""
        return self.id.hex
