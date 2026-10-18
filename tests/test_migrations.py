"""Migrations run down to an empty schema and back up."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from linuxlab.config import get_settings
from linuxlab.db import create_engine

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
# The last revision of increment 08, before user_missions.
BEFORE_PROGRESS = "c4e1a7b39d52"
# The last revision of increment 09, before previous_lab_id.
BEFORE_REPLACEMENT = "7d3f2a91c6e8"


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


def execute(*statements: str, **params: Any) -> list[Any]:
    """Run the statements in one transaction; return the rows of the last one."""

    async def run() -> list[Any]:
        engine = create_engine(get_settings().database_url)
        try:
            async with engine.begin() as connection:
                rows: list[Any] = []
                for statement in statements:
                    result = await connection.execute(text(statement), params)
                    rows = list(result.all()) if result.returns_rows else []
                return rows
        finally:
            await engine.dispose()

    return asyncio.run(run())


TABLES = {
    "users",
    "auth_sessions",
    "lab_sessions",
    "modules",
    "missions",
    "mission_versions",
    "validation_runs",
    "user_missions",
}


def test_downgrade_and_upgrade_again() -> None:
    config = Config(str(ALEMBIC_INI))

    command.upgrade(config, "head")
    assert tables() >= TABLES

    command.downgrade(config, "base")
    assert tables() == {"alembic_version"}

    command.upgrade(config, "head")
    assert tables() >= TABLES


def test_progress_starts_empty_on_a_migrated_database() -> None:
    """Upgrading a database of increment 08 keeps its rows and infers no completion,
    not even from a lab with a passed run."""
    config = Config(str(ALEMBIC_INI))
    command.downgrade(config, "base")
    command.upgrade(config, BEFORE_PROGRESS)
    assert "user_missions" not in tables()
    user_id, lab_id = uuid.uuid4(), uuid.uuid4()
    execute(
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (:user, 'ana@example.com', '$argon2id$synthetic', 'Ana')",
        "INSERT INTO modules (slug, title, description, status)"
        " VALUES ('alpha', 'A', 'A', 'published')",
        "INSERT INTO missions (slug, module_id, position, status, current_version)"
        " SELECT 'sample-file', id, 1, 'published', 1 FROM modules",
        "INSERT INTO mission_versions (mission_id, version, content_hash, spec)"
        " SELECT id, 1, repeat('a', 64), '{}' FROM missions",
        "INSERT INTO lab_sessions (id, user_id, status, created_at, last_activity_at,"
        " expires_at, mission_id, mission_version, params)"
        " SELECT :lab, :user, 'ready', now(), now(), now() + interval '2 hours', id, 1, '{}'"
        " FROM missions",
        "INSERT INTO validation_runs (lab_session_id, mission_id, mission_version, outcome,"
        " result, answer_provided, started_at, finished_at)"
        " SELECT :lab, id, 1, 'passed', '{}', false, now(), now() FROM missions",
        user=user_id,
        lab=lab_id,
    )

    try:
        command.upgrade(config, "head")

        assert execute("SELECT count(*) FROM user_missions") == [(0,)]
        assert execute("SELECT count(*) FROM lab_sessions") == [(1,)]
        assert execute("SELECT outcome FROM validation_runs") == [("passed",)]

        # Down to increment 08 and up again, with the rows still there.
        command.downgrade(config, BEFORE_PROGRESS)
        assert "user_missions" not in tables()
        command.upgrade(config, "head")
        assert execute("SELECT count(*) FROM user_missions") == [(0,)]
    finally:
        command.downgrade(config, "base")
        command.upgrade(config, "head")


def test_downgrading_replacement_keeps_labs_and_their_reasons_valid() -> None:
    """Going back to increment 09 drops the link between labs and records replaced labs
    as ended by their user, the reason closest to theirs that the schema knows."""
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")
    user_id, first, second, third = (uuid.uuid4() for _ in range(4))
    execute(
        "TRUNCATE users, auth_sessions, lab_sessions, validation_runs, user_missions, modules,"
        " missions, mission_versions",
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (:user, 'ana@example.com', '$argon2id$synthetic', 'Ana')",
        "INSERT INTO lab_sessions (id, user_id, status, end_reason, created_at,"
        " last_activity_at, expires_at, ended_at)"
        " VALUES (:first, :user, 'terminated', 'reset', now(), now(),"
        " now() + interval '2 hours', now())",
        "INSERT INTO lab_sessions (id, user_id, status, end_reason, created_at,"
        " last_activity_at, expires_at, ended_at, previous_lab_id)"
        " VALUES (:second, :user, 'terminated', 'mission_switch', now(), now(),"
        " now() + interval '2 hours', now(), :first)",
        "INSERT INTO lab_sessions (id, user_id, status, created_at, last_activity_at,"
        " expires_at, previous_lab_id)"
        " VALUES (:third, :user, 'ready', now(), now(), now() + interval '2 hours', :second)",
        user=user_id,
        first=first,
        second=second,
        third=third,
    )

    try:
        command.downgrade(config, BEFORE_REPLACEMENT)

        assert set(execute("SELECT id, end_reason FROM lab_sessions")) == {
            (first, "user"),
            (second, "user"),
            (third, None),
        }
        columns = execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'lab_sessions' AND column_name = 'previous_lab_id'"
        )
        assert columns == []
        # The guard of increment 09 is back, still attached, and still enforced.
        assert execute("SELECT count(*) FROM pg_trigger WHERE tgname = 'lab_sessions_guard'") == [
            (1,)
        ]
        assert execute("SELECT count(*) FROM pg_proc WHERE proname = 'lab_sessions_guard'") == [
            (1,)
        ]
        with pytest.raises(Exception, match="end reason cannot change"):
            execute("UPDATE lab_sessions SET end_reason = 'oom' WHERE id = :id", id=first)
        with pytest.raises(Exception, match="ck_lab_sessions_end_reason"):
            execute(
                "INSERT INTO lab_sessions (id, user_id, status, end_reason, created_at,"
                " last_activity_at, expires_at, ended_at)"
                " VALUES (:id, :user, 'terminated', 'reset', now(), now(),"
                " now() + interval '2 hours', now())",
                id=uuid.uuid4(),
                user=user_id,
            )

        command.upgrade(config, "head")
        assert execute("SELECT count(*) FROM lab_sessions WHERE previous_lab_id IS NULL") == [(3,)]
    finally:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
