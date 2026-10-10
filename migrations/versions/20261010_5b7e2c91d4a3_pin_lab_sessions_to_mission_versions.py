"""pin lab sessions to mission versions

Revision ID: 5b7e2c91d4a3
Revises: 84369683feef
Create Date: 2026-10-10 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5b7e2c91d4a3"
down_revision: str | None = "84369683feef"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The guard of 028d53a10e19, plus: the pinned mission, version and parameters never
# change once the row exists.
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
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.add_column("lab_sessions", sa.Column("mission_id", sa.BigInteger(), nullable=True))
    op.add_column("lab_sessions", sa.Column("mission_version", sa.Integer(), nullable=True))
    op.add_column(
        "lab_sessions",
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_lab_sessions_mission_id_mission_versions"),
        "lab_sessions",
        "mission_versions",
        ["mission_id", "mission_version"],
        ["mission_id", "version"],
    )
    op.create_check_constraint(
        op.f("ck_lab_sessions_mission_pinned"),
        "lab_sessions",
        "(mission_id IS NULL) = (mission_version IS NULL)"
        " AND (mission_id IS NULL) = (params IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_lab_sessions_params_object"),
        "lab_sessions",
        "params IS NULL OR jsonb_typeof(params) = 'object'",
    )
    op.execute(GUARD_FUNCTION)


def downgrade() -> None:
    op.execute(PREVIOUS_GUARD_FUNCTION)
    op.drop_constraint(op.f("ck_lab_sessions_params_object"), "lab_sessions", type_="check")
    op.drop_constraint(op.f("ck_lab_sessions_mission_pinned"), "lab_sessions", type_="check")
    op.drop_constraint(
        op.f("fk_lab_sessions_mission_id_mission_versions"), "lab_sessions", type_="foreignkey"
    )
    op.drop_column("lab_sessions", "params")
    op.drop_column("lab_sessions", "mission_version")
    op.drop_column("lab_sessions", "mission_id")
