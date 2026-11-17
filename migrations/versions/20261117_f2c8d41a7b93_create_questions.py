"""create questions, question_versions, user_questions and question_attempts

Revision ID: f2c8d41a7b93
Revises: b91e4d7a2c60
Create Date: 2026-11-17 12:00:00.000000

Conceptual questions (docs/questions.md), synced from content/questions/ like missions
from content/missions/, and the answers users give to them.

content_syncs gains the questions' count and fingerprint. Rows of syncs that ran before
this migration get 0 and the SHA-256 of nothing, which is what the fingerprint of a
database without questions is: there were none. The defaults are dropped afterwards, so
every new row states both. Adding the columns does not fire the table's update trigger.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2c8d41a7b93"
down_revision: str | None = "b91e4d7a2c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

GUARDS = {
    # Question versions are immutable, like mission versions.
    "question_versions": (
        "question versions cannot be changed or deleted",
        "UPDATE OR DELETE",
    ),
    # A completion and an attempt are final when inserted. DELETE stays possible so that
    # deleting a user cascades to them.
    "user_questions": ("user questions cannot be changed", "UPDATE"),
    "question_attempts": ("question attempts cannot be changed", "UPDATE"),
}


def _guard(table: str, message: str, events: str) -> None:
    op.execute(
        f"CREATE FUNCTION {table}_immutable() RETURNS trigger LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        f"    RAISE EXCEPTION '{message}' USING ERRCODE = 'check_violation';\n"
        "END\n"
        "$$"
    )
    op.execute(
        f"CREATE TRIGGER {table}_immutable BEFORE {events} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {table}_immutable()"
    )


def upgrade() -> None:
    op.create_table(
        "questions",
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
            name=op.f("ck_questions_position_when_listed"),
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9-]{1,64}$'", name=op.f("ck_questions_slug_format")),
        sa.CheckConstraint(
            "status IN ('archived', 'draft', 'published')", name=op.f("ck_questions_status")
        ),
        sa.CheckConstraint(
            "current_version >= 1", name=op.f("ck_questions_current_version_positive")
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_questions_position_positive")),
        sa.ForeignKeyConstraint(
            ["module_id"], ["modules.id"], name=op.f("fk_questions_module_id_modules")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_questions")),
        sa.UniqueConstraint(
            "module_id",
            "position",
            deferrable=True,
            initially="DEFERRED",
            name="uq_questions_module_id_position",
        ),
        sa.UniqueConstraint("slug", name=op.f("uq_questions_slug")),
    )
    op.create_index(op.f("ix_questions_module_id"), "questions", ["module_id"], unique=False)
    op.create_table(
        "question_versions",
        sa.Column("question_id", sa.BigInteger(), nullable=False),
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
            name=op.f("ck_question_versions_content_hash_format"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_question_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["questions.id"],
            name=op.f("fk_question_versions_question_id_questions"),
        ),
        sa.PrimaryKeyConstraint("question_id", "version", name=op.f("pk_question_versions")),
    )
    # questions and question_versions refer to each other; this side is added last.
    op.create_foreign_key(
        "fk_questions_current_version_question_versions",
        "questions",
        "question_versions",
        ["id", "current_version"],
        ["question_id", "version"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "user_questions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("completed_version", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_questions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id", "completed_version"],
            ["question_versions.question_id", "question_versions.version"],
            name=op.f("fk_user_questions_question_id_question_versions"),
        ),
        sa.PrimaryKeyConstraint("user_id", "question_id", name=op.f("pk_user_questions")),
    )
    op.create_table(
        "question_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("question_version", sa.Integer(), nullable=False),
        sa.Column("option", sa.Text(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column(
            "answered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "option ~ '^[a-z0-9-]{1,32}$'", name=op.f("ck_question_attempts_option_format")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_question_attempts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id", "question_version"],
            ["question_versions.question_id", "question_versions.version"],
            name=op.f("fk_question_attempts_question_id_question_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_attempts")),
    )
    op.create_index(
        "ix_question_attempts_user_id_question_id_question_version",
        "question_attempts",
        ["user_id", "question_id", "question_version"],
        unique=False,
    )
    for table, (message, events) in GUARDS.items():
        _guard(table, message, events)

    op.add_column(
        "content_syncs",
        sa.Column("questions", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "content_syncs",
        sa.Column("question_fingerprint", sa.Text(), server_default=EMPTY_SHA256, nullable=False),
    )
    op.alter_column("content_syncs", "questions", server_default=None)
    op.alter_column("content_syncs", "question_fingerprint", server_default=None)
    op.create_check_constraint(
        op.f("ck_content_syncs_questions_not_negative"), "content_syncs", "questions >= 0"
    )
    op.create_check_constraint(
        op.f("ck_content_syncs_question_fingerprint_format"),
        "content_syncs",
        "question_fingerprint ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_content_syncs_question_fingerprint_format"), "content_syncs", type_="check"
    )
    op.drop_constraint(
        op.f("ck_content_syncs_questions_not_negative"), "content_syncs", type_="check"
    )
    op.drop_column("content_syncs", "question_fingerprint")
    op.drop_column("content_syncs", "questions")
    for table in reversed(GUARDS):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.execute(f"DROP FUNCTION {table}_immutable()")
    op.drop_index(
        "ix_question_attempts_user_id_question_id_question_version",
        table_name="question_attempts",
    )
    op.drop_table("question_attempts")
    op.drop_table("user_questions")
    op.drop_constraint(
        "fk_questions_current_version_question_versions", "questions", type_="foreignkey"
    )
    op.drop_table("question_versions")
    op.drop_index(op.f("ix_questions_module_id"), table_name="questions")
    op.drop_table("questions")
