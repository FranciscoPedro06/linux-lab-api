"""Modules, missions, questions and their versions, as synced from content/.

- modules, missions and questions keep a stable identity (the slug) and their
  publication state. A module, mission or question removed from content/ is
  archived, never deleted.
- mission_versions and question_versions hold the complete specification of each
  version. Rows are never updated or deleted (triggers in the migrations).
- missions.current_version always names an existing version of the same mission, and
  questions.current_version one of the same question.
- A module lists missions or questions, never both (checked by the loader).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from linuxlab.db import Base

STATUSES = ("archived", "draft", "published")
STATUSES_SQL = ", ".join(f"'{status}'" for status in STATUSES)
SLUG_SQL = "slug ~ '^[a-z0-9-]{1,64}$'"


class Module(Base):
    __tablename__ = "modules"
    __table_args__ = (
        CheckConstraint(f"status IN ({STATUSES_SQL})", name="status"),
        CheckConstraint(SLUG_SQL, name="slug_format"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Mission(Base):
    __tablename__ = "missions"
    __table_args__ = (
        CheckConstraint(f"status IN ({STATUSES_SQL})", name="status"),
        CheckConstraint(SLUG_SQL, name="slug_format"),
        CheckConstraint("position >= 1", name="position_positive"),
        # Position in the module's list; none once the mission left content/.
        CheckConstraint("position IS NOT NULL OR status = 'archived'", name="position_when_listed"),
        CheckConstraint("current_version >= 1", name="current_version_positive"),
        # Deferred, so a sync can reorder a module within its transaction.
        UniqueConstraint(
            "module_id",
            "position",
            name="uq_missions_module_id_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        # Deferred, so a mission and its first version can be inserted together.
        ForeignKeyConstraint(
            ["id", "current_version"],
            ["mission_versions.mission_id", "mission_versions.version"],
            name="fk_missions_current_version_mission_versions",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    # The module that lists the mission, or listed it last.
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"), index=True)
    position: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContentSync(Base):
    """One successful sync: the commit it synced, when known, and the missions and
    questions after it.

    Rows are never updated (trigger in the migration that creates the table).
    """

    __tablename__ = "content_syncs"
    __table_args__ = (
        CheckConstraint("source_commit ~ '^[0-9a-f]{40}$'", name="source_commit_format"),
        CheckConstraint("missions >= 0", name="missions_not_negative"),
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="fingerprint_format"),
        CheckConstraint("questions >= 0", name="questions_not_negative"),
        CheckConstraint(
            "question_fingerprint ~ '^[0-9a-f]{64}$'", name="question_fingerprint_format"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The commit of linux-lab-content whose content/ was synced; none when the caller
    # did not name one.
    source_commit: Mapped[str | None] = mapped_column(Text)
    missions: Mapped[int] = mapped_column(Integer)
    # As infra/ops/lib.sh computes it (content_fingerprint), right after the sync.
    fingerprint: Mapped[str] = mapped_column(Text)
    # The same for questions (question_fingerprint in lib.sh). Rows written before
    # questions existed record none: 0 and the SHA-256 of nothing.
    questions: Mapped[int] = mapped_column(Integer)
    question_fingerprint: Mapped[str] = mapped_column(Text)


class MissionVersion(Base):
    __tablename__ = "mission_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    )

    mission_id: Mapped[int] = mapped_column(ForeignKey("missions.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    # SHA-256 of the canonical JSON of spec. Not unique: content that returns to an
    # earlier state becomes a new version.
    content_hash: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Question(Base):
    """A conceptual question (docs/questions.md), listed by a module of questions.

    Same rules as missions: a stable slug, the module and position it is listed at,
    archived (never deleted) when it leaves content/, and a current version.
    """

    __tablename__ = "questions"
    __table_args__ = (
        CheckConstraint(f"status IN ({STATUSES_SQL})", name="status"),
        CheckConstraint(SLUG_SQL, name="slug_format"),
        CheckConstraint("position >= 1", name="position_positive"),
        CheckConstraint("position IS NOT NULL OR status = 'archived'", name="position_when_listed"),
        CheckConstraint("current_version >= 1", name="current_version_positive"),
        UniqueConstraint(
            "module_id",
            "position",
            name="uq_questions_module_id_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["id", "current_version"],
            ["question_versions.question_id", "question_versions.version"],
            name="fk_questions_current_version_question_versions",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    module_id: Mapped[int] = mapped_column(ForeignKey("modules.id"), index=True)
    position: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuestionVersion(Base):
    """Every version of a question, never updated or deleted (trigger in the migration).

    spec holds the answer and the feedback of each option: only the answer endpoint
    reads them, to check an answer and to say why.
    """

    __tablename__ = "question_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_format"),
    )

    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
