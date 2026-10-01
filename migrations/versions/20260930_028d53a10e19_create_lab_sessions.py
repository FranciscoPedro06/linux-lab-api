"""create lab sessions

Revision ID: 028d53a10e19
Revises: 9d2012fbfc42
Create Date: 2026-09-30 21:33:42.507869
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "028d53a10e19"
down_revision: str | None = "9d2012fbfc42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Lifecycle rules enforced by the database (see src/linuxlab/labs/models.py): only the
# listed status transitions, and neither the owner nor a recorded end reason changes.
GUARD_FUNCTION = """
CREATE FUNCTION lab_sessions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> OLD.status AND NOT (
           (OLD.status = 'provisioning' AND NEW.status IN ('ready', 'failed', 'terminating'))
        OR (OLD.status = 'ready' AND NEW.status = 'terminating')
        OR (OLD.status IN ('terminating', 'failed') AND NEW.status = 'terminated')
    ) THEN
        RAISE EXCEPTION 'invalid lab status transition: % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.id <> OLD.id OR NEW.user_id <> OLD.user_id OR NEW.created_at <> OLD.created_at
       OR NEW.expires_at <> OLD.expires_at THEN
        RAISE EXCEPTION 'lab session identity cannot change' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.end_reason IS NOT NULL AND NEW.end_reason IS DISTINCT FROM OLD.end_reason THEN
        RAISE EXCEPTION 'lab session end reason cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "lab_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status IN ('provisioning', 'ready')) = (end_reason IS NULL)",
            name=op.f("ck_lab_sessions_end_reason_when_ended"),
        ),
        sa.CheckConstraint(
            "(status IN ('provisioning', 'ready')) = (ended_at IS NULL)",
            name=op.f("ck_lab_sessions_ended_at_when_ended"),
        ),
        sa.CheckConstraint(
            "end_reason IN ('container_lost', 'logout', 'max_lifetime', 'no_input', "
            "'no_terminal', 'oom', 'provisioning_failed', 'provisioning_timeout', 'user')",
            name=op.f("ck_lab_sessions_end_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('failed', 'provisioning', 'ready', 'terminated', 'terminating')",
            name=op.f("ck_lab_sessions_status"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_lab_sessions_expires_after_creation")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_lab_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_sessions")),
    )
    op.create_index(
        "ix_lab_sessions_user_id_created_at",
        "lab_sessions",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "one_active_lab_per_user",
        "lab_sessions",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('provisioning', 'ready', 'terminating')"),
    )
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER lab_sessions_guard BEFORE UPDATE ON lab_sessions "
        "FOR EACH ROW EXECUTE FUNCTION lab_sessions_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER lab_sessions_guard ON lab_sessions")
    op.execute("DROP FUNCTION lab_sessions_guard()")
    op.drop_index(
        "one_active_lab_per_user",
        table_name="lab_sessions",
        postgresql_where=sa.text("status IN ('provisioning', 'ready', 'terminating')"),
    )
    op.drop_index("ix_lab_sessions_user_id_created_at", table_name="lab_sessions")
    op.drop_table("lab_sessions")
