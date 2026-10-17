"""user_missions: the first completion of a mission by a user.

- One row per user and mission (primary key), inserted by the validation that first
  passed for that mission and never changed afterwards (trigger in the migration).
  DELETE stays possible so that deleting a user cascades to their progress.
- Progress belongs to the mission, not to a version: completed_version records the
  version the first completion was on, through a foreign key to mission_versions,
  so it exists and belongs to the mission. A newer version of the mission does not
  undo the completion.
- No row means no completion is recorded, not that the mission was never started:
  labs record that.
- Nothing refers to lab_sessions or validation_runs, so progress outlives the labs
  and runs it came from.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, ForeignKeyConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column

from linuxlab.db import Base


class UserMission(Base):
    __tablename__ = "user_missions"
    __table_args__ = (
        # The completed version belongs to the mission: both columns reference the
        # version's key.
        ForeignKeyConstraint(
            ["mission_id", "completed_version"],
            ["mission_versions.mission_id", "mission_versions.version"],
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    mission_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The version of the lab whose validation first passed.
    completed_version: Mapped[int] = mapped_column(Integer)
    # finished_at of that validation run.
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
