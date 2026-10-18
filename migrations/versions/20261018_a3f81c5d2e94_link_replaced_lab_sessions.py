"""link replaced lab sessions

Revision ID: a3f81c5d2e94
Revises: 7d3f2a91c6e8
Create Date: 2026-10-18 12:00:00.000000

A lab created to replace another (reset or mission switch) names it in
previous_lab_id. The previous lab belongs to the same user, has at most one
successor, and the link never changes. Two end reasons record why a lab was
replaced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f81c5d2e94"
down_revision: str | None = "7d3f2a91c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

END_REASONS = (
    "'container_lost', 'logout', 'max_lifetime', 'mission_switch', 'no_input', "
    "'no_terminal', 'oom', 'provisioning_failed', 'provisioning_timeout', 'reset', 'user'"
)
PREVIOUS_END_REASONS = (
    "'container_lost', 'logout', 'max_lifetime', 'no_input', "
    "'no_terminal', 'oom', 'provisioning_failed', 'provisioning_timeout', 'user'"
)

# The guard of 5b7e2c91d4a3, plus: the previous lab never changes once the row exists.
GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION lab_sessions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NEW.mission_id IS DISTINCT FROM OLD.mission_id
       OR NEW.mission_version IS DISTINCT FROM OLD.mission_version
       OR NEW.params IS DISTINCT FROM OLD.params THEN
        RAISE EXCEPTION 'lab session mission cannot change' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.previous_lab_id IS DISTINCT FROM OLD.previous_lab_id THEN
        RAISE EXCEPTION 'lab session previous lab cannot change'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$
"""

PREVIOUS_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION lab_sessions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF NEW.mission_id IS DISTINCT FROM OLD.mission_id
       OR NEW.mission_version IS DISTINCT FROM OLD.mission_version
       OR NEW.params IS DISTINCT FROM OLD.params THEN
        RAISE EXCEPTION 'lab session mission cannot change' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.drop_constraint(op.f("ck_lab_sessions_end_reason"), "lab_sessions", type_="check")
    op.create_check_constraint(
        op.f("ck_lab_sessions_end_reason"), "lab_sessions", f"end_reason IN ({END_REASONS})"
    )
    op.add_column("lab_sessions", sa.Column("previous_lab_id", sa.Uuid(), nullable=True))
    # Target of the foreign key below. The id alone is already unique.
    op.create_unique_constraint("uq_lab_sessions_id_user_id", "lab_sessions", ["id", "user_id"])
    # The previous lab belongs to the same user. MATCH SIMPLE: a lab without a
    # previous one is not checked. Deleting a user deletes all their labs in one
    # statement, so the check at its end finds no dangling reference.
    op.create_foreign_key(
        "fk_lab_sessions_previous_lab_id_lab_sessions",
        "lab_sessions",
        "lab_sessions",
        ["previous_lab_id", "user_id"],
        ["id", "user_id"],
    )
    op.create_check_constraint(
        op.f("ck_lab_sessions_not_its_own_previous"),
        "lab_sessions",
        "previous_lab_id IS NULL OR previous_lab_id <> id",
    )
    op.create_index(
        "one_successor_per_lab",
        "lab_sessions",
        ["previous_lab_id"],
        unique=True,
        postgresql_where=sa.text("previous_lab_id IS NOT NULL"),
    )
    op.execute(GUARD_FUNCTION)


def downgrade() -> None:
    op.execute(PREVIOUS_GUARD_FUNCTION)
    op.drop_index(
        "one_successor_per_lab",
        table_name="lab_sessions",
        postgresql_where=sa.text("previous_lab_id IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_lab_sessions_not_its_own_previous"), "lab_sessions", type_="check")
    op.drop_constraint(
        "fk_lab_sessions_previous_lab_id_lab_sessions", "lab_sessions", type_="foreignkey"
    )
    op.drop_constraint("uq_lab_sessions_id_user_id", "lab_sessions", type_="unique")
    op.drop_column("lab_sessions", "previous_lab_id")
    # The previous schema has no reason for a replaced lab. The user asked for the
    # replacement, so those labs are recorded as ended by the user. The guard refuses
    # changing a recorded reason; it is set aside for this one statement.
    op.execute("ALTER TABLE lab_sessions DISABLE TRIGGER lab_sessions_guard")
    op.execute(
        "UPDATE lab_sessions SET end_reason = 'user'"
        " WHERE end_reason IN ('reset', 'mission_switch')"
    )
    op.execute("ALTER TABLE lab_sessions ENABLE TRIGGER lab_sessions_guard")
    op.drop_constraint(op.f("ck_lab_sessions_end_reason"), "lab_sessions", type_="check")
    op.create_check_constraint(
        op.f("ck_lab_sessions_end_reason"),
        "lab_sessions",
        f"end_reason IN ({PREVIOUS_END_REASONS})",
    )
