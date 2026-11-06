"""create content_syncs

Revision ID: b91e4d7a2c60
Revises: e6b40c7d9a18
Create Date: 2026-11-05 12:00:00.000000

One row per successful `linuxlab content sync`: when it ran, the commit of
linux-lab-content it synced when the caller named one (infra/ops/content-sync.sh
does), and the missions' fingerprint right after it, as infra/ops/lib.sh computes it.
A backup records the commit only while that fingerprint is still the database's.

No backfill: the commit of a sync that ran before this migration was never stored,
and nothing is inferred from the missions. Rows are final when inserted.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b91e4d7a2c60"
down_revision: str | None = "e6b40c7d9a18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARD_FUNCTION = """
CREATE FUNCTION content_syncs_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'content syncs cannot be changed' USING ERRCODE = 'check_violation';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "content_syncs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("source_commit", sa.Text(), nullable=True),
        sa.Column("missions", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_commit ~ '^[0-9a-f]{40}$'", name=op.f("ck_content_syncs_source_commit_format")
        ),
        sa.CheckConstraint("missions >= 0", name=op.f("ck_content_syncs_missions_not_negative")),
        sa.CheckConstraint(
            "fingerprint ~ '^[0-9a-f]{64}$'", name=op.f("ck_content_syncs_fingerprint_format")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_syncs")),
    )
    op.execute(GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER content_syncs_immutable BEFORE UPDATE ON content_syncs "
        "FOR EACH ROW EXECUTE FUNCTION content_syncs_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER content_syncs_immutable ON content_syncs")
    op.execute("DROP FUNCTION content_syncs_immutable()")
    op.drop_table("content_syncs")
