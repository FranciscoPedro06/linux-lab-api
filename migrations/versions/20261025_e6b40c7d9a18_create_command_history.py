"""create command_history

Revision ID: e6b40c7d9a18
Revises: a3f81c5d2e94
Create Date: 2026-10-25 12:00:00.000000

No backfill: no command was recorded before this migration, and nothing is inferred
from labs or validation runs. History starts with the first command observed after it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b40c7d9a18"
down_revision: str | None = "a3f81c5d2e94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Entries are final when inserted. DELETE stays possible so that removing a lab, or the
# user who owns it, cascades to its history.
GUARD_FUNCTION = """
CREATE FUNCTION command_history_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'command history cannot be changed' USING ERRCODE = 'check_violation';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "command_history",
        sa.Column("lab_session_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq > 0", name=op.f("ck_command_history_seq_positive")),
        sa.CheckConstraint(
            "octet_length(command) BETWEEN 1 AND 4096",
            name=op.f("ck_command_history_command_length"),
        ),
        sa.CheckConstraint(
            r"command !~ '[\x01-\x08\x0b-\x1f\x7f-\x9f]'",
            name=op.f("ck_command_history_command_characters"),
        ),
        sa.ForeignKeyConstraint(
            ["lab_session_id"],
            ["lab_sessions.id"],
            name=op.f("fk_command_history_lab_session_id_lab_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("lab_session_id", "seq", name=op.f("pk_command_history")),
    )
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER command_history_immutable BEFORE UPDATE ON command_history "
        "FOR EACH ROW EXECUTE FUNCTION command_history_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER command_history_immutable ON command_history")
    op.execute("DROP FUNCTION command_history_immutable()")
    op.drop_table("command_history")
