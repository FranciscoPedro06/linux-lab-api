"""Modules, missions and mission versions, as synced from content/.

- modules and missions keep a stable identity (the slug) and their publication
  state. A module or mission removed from content/ is archived, never deleted.
- mission_versions holds the complete specification of each version. Rows are never
  updated or deleted (trigger in the migration that creates the table).
- missions.current_version always names an existing version of the same mission.
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
