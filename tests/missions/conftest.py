"""Fixtures of the mission harness.

Every content directory the harness tests is synced into a database of its own,
created for the test session on the PostgreSQL server of DATABASE_URL and dropped
at the end: the harness never writes to the database DATABASE_URL names, and a
sync of one directory never archives the missions of another.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiodocker
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from linuxlab.config import get_settings
from linuxlab.content.loader import ContentInvalid, load_content
from linuxlab.db import create_engine, create_sessionmaker
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.models import ACTIVE_STATUSES, EndReason, LabSession
from linuxlab.labs.runtime import LabRuntime
from linuxlab.labs.terminal.relay import TerminalRegistry
from tests.labs.support import LAB_IMAGE

from .harness import Case, CaseKind, Harness, MissionUnderTest, prepare

REPOSITORY = Path(__file__).resolve().parents[2]
# Missions that exist only to show what the harness detects.
HARNESS_FIXTURE = REPOSITORY / "tests" / "fixtures" / "harness"


class ContentDirError(Exception):
    pass


def content_dir() -> Path:
    """The content directory under test, from CONTENT_DIR in the environment.

    There is no default: mission content is private and lives outside this
    repository, so an unset variable is an error rather than an empty catalog. A
    directory inside this repository is refused too, so private content is never
    placed where a commit, the image build context or an artifact could take it.
    """
    value = os.environ.get("CONTENT_DIR")
    if not value:
        raise ContentDirError(
            "CONTENT_DIR is not set; point it to the content/ directory of the private "
            "mission content (docs/missions.md#content-source)"
        )
    root = Path(value).resolve()
    if root.is_relative_to(REPOSITORY):
        raise ContentDirError(
            f"CONTENT_DIR must be outside this repository, got {root} "
            "(docs/missions.md#content-source)"
        )
    return root


def content_case_ids(root: Path) -> list[str]:
    """`slug:solution-1`, ... for every case of the content in `root`, read at
    collection time. Invalid or missing content gives none; the content tests
    report it."""
    try:
        content = load_content(root)
    except (ContentInvalid, OSError):
        return []
    return [
        f"{mission.slug}:{kind}-{index + 1}"
        for mission in content.missions
        for kind, key in (
            (CaseKind.SOLUTION, "solutions"),
            (CaseKind.COUNTEREXAMPLE, "counterexamples"),
        )
        for index in range(len(mission.spec[key]))
    ]


@dataclass
class Prepared:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    missions: dict[str, MissionUnderTest]

    def case(self, case_id: str) -> Case:
        """The case named `slug:solution-1`."""
        slug, name = case_id.split(":")
        kind, position = name.rsplit("-", 1)
        return Case(self.missions[slug], CaseKind(kind), int(position) - 1)


class Databases:
    def __init__(self) -> None:
        self._prepared: dict[Path, Prepared] = {}
        self._names: list[str] = []
        url = make_url(get_settings().database_url)
        self._url = url
        self._admin = create_async_engine(url, isolation_level="AUTOCOMMIT")

    async def prepared(self, root: Path) -> Prepared:
        """The content of `root`, synced into a new migrated database."""
        if root not in self._prepared:
            name = f"linuxlab_harness_{uuid.uuid4().hex[:12]}"
            async with self._admin.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
            self._names.append(name)
            url = self._url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(_migrate, url)
            engine = create_engine(url)
            sessionmaker = create_sessionmaker(engine)
            missions = {mission.slug: mission for mission in await prepare(sessionmaker, root)}
            self._prepared[root] = Prepared(engine, sessionmaker, missions)
        return self._prepared[root]

    async def close(self) -> None:
        for prepared in self._prepared.values():
            await prepared.engine.dispose()
        async with self._admin.connect() as connection:
            for name in self._names:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await self._admin.dispose()


def _migrate(url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPOSITORY,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
async def databases() -> AsyncIterator[Databases]:
    databases = Databases()
    yield databases
    await databases.close()


@asynccontextmanager
async def harness_for(prepared: Prepared, runtime: LabRuntime) -> AsyncIterator[Harness]:
    """A harness on its own deployment. Afterwards every lab it left active is ended,
    and every container of the deployment removed."""
    labs = Labs(
        prepared.sessionmaker,
        runtime,
        TerminalRegistry(),
        image=LAB_IMAGE,
        deployment=f"missions-{uuid.uuid4().hex[:12]}",
        capacity=10,
    )
    try:
        yield Harness(prepared.sessionmaker, runtime, labs)
    finally:
        async with prepared.sessionmaker() as db:
            active = (
                await db.scalars(
                    select(LabSession.id).where(LabSession.status.in_(ACTIVE_STATUSES))
                )
            ).all()
        for lab_id in active:
            await labs.end(lab_id, EndReason.USER)
        await labs.reconcile(startup=True)


async def active_labs(prepared: Prepared) -> int:
    async with prepared.sessionmaker() as db:
        rows = await db.scalars(select(LabSession.id).where(LabSession.status.in_(ACTIVE_STATUSES)))
        return len(rows.all())


@pytest.fixture(scope="session")
async def docker_client() -> AsyncIterator[aiodocker.Docker]:
    client = aiodocker.Docker()
    yield client
    await client.close()
