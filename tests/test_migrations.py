"""Migrations run down to an empty schema and back up."""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from linuxlab.config import get_settings
from linuxlab.db import create_engine

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def tables() -> set[str]:
    async def read() -> set[str]:
        engine = create_engine(get_settings().database_url)
        try:
            async with engine.connect() as connection:
                names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        finally:
            await engine.dispose()
        return set(names)

    return asyncio.run(read())


TABLES = {"users", "auth_sessions", "lab_sessions", "modules", "missions", "mission_versions"}


def test_downgrade_and_upgrade_again() -> None:
    config = Config(str(ALEMBIC_INI))

    command.upgrade(config, "head")
    assert tables() >= TABLES

    command.downgrade(config, "base")
    assert tables() == {"alembic_version"}

    command.upgrade(config, "head")
    assert tables() >= TABLES
