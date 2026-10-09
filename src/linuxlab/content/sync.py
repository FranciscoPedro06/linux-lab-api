"""Sync validated content into modules, missions and mission_versions.

content/ is the source of truth; the database is its queryable copy, plus every
version that was ever published. One sync is one transaction, serialized with any
other sync by an advisory lock taken before the current state is read:

- modules and missions are matched by slug, created when new and updated in place
  (title, status, module, position) without creating a version;
- a mission gets a new version, numbered after its highest one, only when its
  content hash differs from that of its current version;
- modules and missions missing from content/ are archived, never deleted, and keep
  their versions;
- an error anywhere rolls the whole sync back.

Running it again on the same content changes nothing.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linuxlab.content import loader
from linuxlab.content.loader import Content
from linuxlab.content.models import Mission, MissionVersion, Module

SYNC_LOCK = 0x6D69_7373  # "miss"
ARCHIVED = "archived"


class EmptyContent(Exception):
    """content/ has no modules and no missions, and archiving everything was not asked for."""


@dataclass
class SyncReport:
    modules_created: int = 0
    modules_updated: int = 0
    modules_archived: int = 0
    missions_created: int = 0
    missions_updated: int = 0
    missions_archived: int = 0
    versions_created: int = 0

    @property
    def changed(self) -> bool:
        return any(getattr(self, field.name) for field in fields(self))


async def sync_content(
    sessionmaker: async_sessionmaker[AsyncSession],
    content: Content,
    *,
    allow_empty: bool = False,
) -> SyncReport:
    """Make the database match `content`. With empty content, refuses unless `allow_empty`."""
    if content.empty and not allow_empty:
        raise EmptyContent
    report = SyncReport()
    async with sessionmaker() as db, db.begin():
        # Taken before reading anything, so version numbers are decided on the state
        # left by any sync that ran first.
        await db.execute(select(func.pg_advisory_xact_lock(SYNC_LOCK)))
        module_ids = await _sync_modules(db, content, report)
        await _sync_missions(db, content, module_ids, report)
    return report


async def _sync_modules(db: AsyncSession, content: Content, report: SyncReport) -> dict[str, int]:
    rows = {row.slug: row for row in await db.scalars(select(Module))}
    for module in content.modules:
        row = rows.get(module.slug)
        values = {"title": module.title, "description": module.description, "status": module.status}
        if row is None:
            row = Module(slug=module.slug, **values)
            db.add(row)
            rows[module.slug] = row
            report.modules_created += 1
        elif _update(row, values):
            report.modules_updated += 1
    listed = {module.slug for module in content.modules}
    for slug, row in rows.items():
        if slug not in listed and row.status != ARCHIVED:
            row.status = ARCHIVED
            report.modules_archived += 1
    await db.flush()
    return {slug: row.id for slug, row in rows.items()}


async def _sync_missions(
    db: AsyncSession, content: Content, module_ids: dict[str, int], report: SyncReport
) -> None:
    rows = {row.slug: row for row in await db.scalars(select(Mission))}
    # A Result has keys(), so dict() would read it as a mapping.
    current = await db.execute(
        select(MissionVersion.mission_id, MissionVersion.content_hash).join(
            Mission,
            (Mission.id == MissionVersion.mission_id)
            & (Mission.current_version == MissionVersion.version),
        )
    )
    current_hashes = {mission_id: content_hash for mission_id, content_hash in current}
    highest = await db.execute(
        select(MissionVersion.mission_id, func.max(MissionVersion.version)).group_by(
            MissionVersion.mission_id
        )
    )
    latest = {mission_id: version for mission_id, version in highest}

    for mission in content.missions:
        row = rows.get(mission.slug)
        values = {
            "module_id": module_ids[mission.module],
            "position": mission.position,
            "status": mission.status,
        }
        if row is None:
            # current_version refers to a version inserted right after; the foreign
            # key is checked at commit.
            row = Mission(slug=mission.slug, current_version=1, **values)
            db.add(row)
            await db.flush()
            db.add(MissionVersion(mission_id=row.id, version=1, **_version(mission)))
            report.missions_created += 1
            report.versions_created += 1
            continue
        if _update(row, values):
            report.missions_updated += 1
        if current_hashes.get(row.id) != mission.content_hash:
            version = latest.get(row.id, 0) + 1
            db.add(MissionVersion(mission_id=row.id, version=version, **_version(mission)))
            await db.flush()
            row.current_version = version
            report.versions_created += 1

    listed = {mission.slug for mission in content.missions}
    for slug, row in rows.items():
        if slug not in listed and (row.status != ARCHIVED or row.position is not None):
            row.status = ARCHIVED
            row.position = None
            report.missions_archived += 1
    await db.flush()


def _version(mission: loader.Mission) -> dict[str, Any]:
    return {"content_hash": mission.content_hash, "spec": mission.spec}


def _update(row: object, values: Mapping[str, object]) -> bool:
    changed = False
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed
