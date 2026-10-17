"""create user_missions

Revision ID: 7d3f2a91c6e8
Revises: c4e1a7b39d52
Create Date: 2026-10-17 18:00:00.000000

No backfill: neither a lab nor a stored run is taken as a completion. Progress
starts with the first validation that passes after this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d3f2a91c6e8"
down_revision: str | None = "c4e1a7b39d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A completion is final when inserted. DELETE stays possible so that deleting a user
# cascades to their progress.
GUARD_FUNCTION = """
CREATE FUNCTION user_missions_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'user missions cannot be changed' USING ERRCODE = 'check_violation';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "user_missions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("mission_id", sa.BigInteger(), nullable=False),
        sa.Column("completed_version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_missions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mission_id", "completed_version"],
            ["mission_versions.mission_id", "mission_versions.version"],
            name=op.f("fk_user_missions_mission_id_mission_versions"),
        ),
        sa.PrimaryKeyConstraint("user_id", "mission_id", name=op.f("pk_user_missions")),
    )
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER user_missions_immutable BEFORE UPDATE ON user_missions "
        "FOR EACH ROW EXECUTE FUNCTION user_missions_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER user_missions_immutable ON user_missions")
    op.execute("DROP FUNCTION user_missions_immutable()")
    op.drop_table("user_missions")
