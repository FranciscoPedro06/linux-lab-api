"""command_history: command lines accepted by the interactive shell of a lab.

- A row belongs to one lab, through (lab_session_id, seq), its primary key. The owner
  is the lab's owner; the table has no user column, so access always goes through
  the lab.
- seq starts at 1 and grows by one per lab, in the order the rows were committed. It
  is assigned by the insert itself (linuxlab.history.service), never by a counter
  in memory.
- `command` is the line as bash recorded it in its own history, between 1 and 4096
  bytes in UTF-8, with no control characters other than tab and newline.
- recorded_at is when the API observed the line, not when the command ran.
- Rows are final when inserted and never updated (trigger in the migration). DELETE
  stays possible so that removing a lab (and with it a user) cascades.
- Nothing refers to validation_runs or user_missions: the history plays no part in
  validation or progress.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from linuxlab.db import Base

MAX_COMMAND_BYTES = 4096

# C0 controls other than tab and newline, DEL and C1 controls, as a PostgreSQL regex.
CONTROL_CHARACTERS_SQL = r"[\x01-\x08\x0b-\x1f\x7f-\x9f]"


class CommandHistoryEntry(Base):
    __tablename__ = "command_history"
    __table_args__ = (
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint(
            f"octet_length(command) BETWEEN 1 AND {MAX_COMMAND_BYTES}", name="command_length"
        ),
        CheckConstraint(f"command !~ '{CONTROL_CHARACTERS_SQL}'", name="command_characters"),
    )

    lab_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lab_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
