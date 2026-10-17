"""create validation_runs

Revision ID: c4e1a7b39d52
Revises: 5b7e2c91d4a3
Create Date: 2026-10-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4e1a7b39d52"
down_revision: str | None = "5b7e2c91d4a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Runs are final when inserted. DELETE stays possible so that deleting a user (and
# with it their labs) cascades to their runs.
GUARD_FUNCTION = """
CREATE FUNCTION validation_runs_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'validation runs cannot be changed' USING ERRCODE = 'check_violation';
END
$$
"""

OUTCOMES = "'error', 'failed', 'passed'"
ERROR_CODES = (
    "'check_failed', 'container_gone', 'invalid_probe_output', 'invalid_spec',"
    " 'probe_failed', 'probe_timeout', 'runtime_unavailable'"
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_lab_sessions_id_mission_id_mission_version",
        "lab_sessions",
        ["id", "mission_id", "mission_version"],
    )
    op.create_table(
        "validation_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("lab_session_id", sa.Uuid(), nullable=False),
        sa.Column("mission_id", sa.BigInteger(), nullable=False),
        sa.Column("mission_version", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("answer_provided", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"outcome IN ({OUTCOMES})", name=op.f("ck_validation_runs_outcome")),
        sa.CheckConstraint(
            f"error_code IN ({ERROR_CODES})", name=op.f("ck_validation_runs_error_code")
        ),
        sa.CheckConstraint(
            "(outcome = 'error') = (error_code IS NOT NULL)",
            name=op.f("ck_validation_runs_error_code_when_error"),
        ),
        sa.CheckConstraint(
            "(result IS NULL) = (error_code IS NOT NULL AND error_code = 'invalid_spec')",
            name=op.f("ck_validation_runs_result_present"),
        ),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name=op.f("ck_validation_runs_result_object"),
        ),
        sa.CheckConstraint(
            "finished_at >= started_at", name=op.f("ck_validation_runs_finished_after_start")
        ),
        sa.ForeignKeyConstraint(
            ["lab_session_id", "mission_id", "mission_version"],
            ["lab_sessions.id", "lab_sessions.mission_id", "lab_sessions.mission_version"],
            name="fk_validation_runs_lab_session_id_lab_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mission_id", "mission_version"],
            ["mission_versions.mission_id", "mission_versions.version"],
            name="fk_validation_runs_mission_id_mission_versions",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_validation_runs")),
    )
    op.create_index(
        "ix_validation_runs_lab_session_id_started_at",
        "validation_runs",
        ["lab_session_id", "started_at"],
    )
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER validation_runs_immutable BEFORE UPDATE ON validation_runs "
        "FOR EACH ROW EXECUTE FUNCTION validation_runs_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER validation_runs_immutable ON validation_runs")
    op.execute("DROP FUNCTION validation_runs_immutable()")
    op.drop_index("ix_validation_runs_lab_session_id_started_at", table_name="validation_runs")
    op.drop_table("validation_runs")
    op.drop_constraint(
        "uq_lab_sessions_id_mission_id_mission_version", "lab_sessions", type_="unique"
    )
