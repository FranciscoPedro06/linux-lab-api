"""create modules, missions and mission versions

Revision ID: 84369683feef
Revises: 028d53a10e19
Create Date: 2026-10-08 22:30:53.732009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "84369683feef"
down_revision: str | None = "028d53a10e19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mission versions are immutable (see src/linuxlab/content/models.py).
GUARD_FUNCTION = """
CREATE FUNCTION mission_versions_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'mission versions cannot be changed or deleted'
        USING ERRCODE = 'check_violation';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "modules",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]{1,64}$'", name=op.f("ck_modules_slug_format")),
        sa.CheckConstraint(
            "status IN ('archived', 'draft', 'published')", name=op.f("ck_modules_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_modules")),
        sa.UniqueConstraint("slug", name=op.f("uq_modules_slug")),
    )
    op.create_table(
        "missions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("module_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "position IS NOT NULL OR status = 'archived'",
            name=op.f("ck_missions_position_when_listed"),
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]{1,64}$'", name=op.f("ck_missions_slug_format")),
        sa.CheckConstraint(
            "status IN ('archived', 'draft', 'published')", name=op.f("ck_missions_status")
        ),
        sa.CheckConstraint(
            "current_version >= 1", name=op.f("ck_missions_current_version_positive")
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_missions_position_positive")),
        sa.ForeignKeyConstraint(
            ["module_id"], ["modules.id"], name=op.f("fk_missions_module_id_modules")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_missions")),
        sa.UniqueConstraint(
            "module_id",
            "position",
            deferrable=True,
            initially="DEFERRED",
            name="uq_missions_module_id_position",
        ),
        sa.UniqueConstraint("slug", name=op.f("uq_missions_slug")),
    )
    op.create_index(op.f("ix_missions_module_id"), "missions", ["module_id"], unique=False)
    op.create_table(
        "mission_versions",
        sa.Column("mission_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_mission_versions_content_hash_format"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_mission_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["mission_id"],
            ["missions.id"],
            name=op.f("fk_mission_versions_mission_id_missions"),
        ),
        sa.PrimaryKeyConstraint("mission_id", "version", name=op.f("pk_mission_versions")),
    )
    # missions and mission_versions refer to each other; this side is added last.
    op.create_foreign_key(
        "fk_missions_current_version_mission_versions",
        "missions",
        "mission_versions",
        ["id", "current_version"],
        ["mission_id", "version"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER mission_versions_immutable BEFORE UPDATE OR DELETE ON mission_versions "
        "FOR EACH ROW EXECUTE FUNCTION mission_versions_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER mission_versions_immutable ON mission_versions")
    op.execute("DROP FUNCTION mission_versions_immutable()")
    op.drop_constraint(
        "fk_missions_current_version_mission_versions", "missions", type_="foreignkey"
    )
    op.drop_table("mission_versions")
    op.drop_index(op.f("ix_missions_module_id"), table_name="missions")
    op.drop_table("missions")
    op.drop_table("modules")
