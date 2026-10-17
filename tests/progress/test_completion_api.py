"""Completions recorded by POST /api/labs/{id}/validate, against PostgreSQL.

Labs run on a FakeRuntime and the probe is answered by the stand-in in
tests/validation/fake_probe.py: progress does not depend on the lab runtime.
"""

import asyncio
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from linuxlab.labs.runtime import ContainerNotRunningError
from linuxlab.validation.service import Run, Validations
from tests.auth.support import run, sql
from tests.content.support import copy_fixture, edit_yaml, mission_file, module_file
from tests.labs.lab_support import (
    MISSION,
    create_lab,
    end_lab,
    fake_runtime,
    ready_lab,
    run_async,
    user,
)
from tests.validation.fake_probe import LabState, install
from tests.validation.test_validate_api import (
    SCRIPT,
    failing,
    lab_params,
    runs,
    solved_state,
    validate,
)

pytestmark = pytest.mark.integration

NOT_COMPLETED = {"status": "not_completed", "completed_version": None, "completed_at": None}
# A completion recorded by another transaction in the concurrency tests.
OTHER_COMPLETION = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def completions(client: TestClient) -> list[Any]:
    return sql(
        client,
        "SELECT u.email, m.slug, p.completed_version, p.completed_at FROM user_missions p"
        " JOIN users u ON u.id = p.user_id JOIN missions m ON m.id = p.mission_id"
        " ORDER BY u.email, m.slug",
    )


def user_id(client: TestClient, email: str) -> uuid.UUID:
    value: uuid.UUID = sql(client, "SELECT id FROM users WHERE email = :e", e=email)[0][0]
    return value


def mission_id(client: TestClient, slug: str = MISSION) -> int:
    value: int = sql(client, "SELECT id FROM missions WHERE slug = :s", s=slug)[0][0]
    return value


def completed(version: int, at: datetime) -> dict[str, Any]:
    return {"status": "completed", "completed_version": version, "completed_at": at}


def body_progress(body: dict[str, Any]) -> dict[str, Any]:
    """The response's progress, with completed_at parsed."""
    progress = dict(body["progress"])
    if progress["completed_at"] is not None:
        progress["completed_at"] = datetime.fromisoformat(progress["completed_at"])
    return progress


def solve(client: TestClient, lab: dict[str, Any]) -> None:
    install(fake_runtime(client), solved_state(lab_params(client, lab["id"])["token"]))


def sync(client: TestClient, root: Path) -> None:
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, load_content(root)))


@pytest.fixture
def setup(client: TestClient) -> tuple[TestClient, str, dict[str, Any]]:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    return client, token, lab


def test_a_failed_run_without_progress_reports_not_completed(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    install(fake_runtime(client), LabState())

    body = validate(client, token, lab["id"]).json()

    assert body["outcome"] == "failed"
    assert body["newly_completed"] is False
    assert body["progress"] == NOT_COMPLETED
    assert completions(client) == []


def test_the_first_pass_records_the_completion_at_the_runs_end(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    solve(client, lab)

    response = validate(client, token, lab["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body) == ["run_id", "outcome", "newly_completed", "mission", "progress", "result"]
    assert body["outcome"] == "passed"
    assert body["newly_completed"] is True
    assert body["mission"] == {"slug": MISSION, "version": 1}
    (run_row,) = runs(client)
    finished_at = run_row[9]
    assert body_progress(body) == completed(1, finished_at)
    assert completions(client) == [("ana@example.com", MISSION, 1, finished_at)]


def test_a_repeated_pass_keeps_the_first_completion(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    solve(client, lab)
    first = validate(client, token, lab["id"]).json()

    second = validate(client, token, lab["id"]).json()

    assert (first["newly_completed"], second["newly_completed"]) == (True, False)
    assert second["outcome"] == "passed"
    assert second["progress"] == first["progress"]
    assert len(runs(client)) == 2
    assert completions(client) == [("ana@example.com", MISSION, 1, runs(client)[0][9])]


@pytest.mark.parametrize("outcome", ["failed", "probe_failed", "container_gone"])
def test_failed_and_error_runs_record_nothing(
    setup: tuple[TestClient, str, dict[str, Any]], outcome: str
) -> None:
    client, token, lab = setup
    if outcome == "failed":
        install(fake_runtime(client), LabState())
    elif outcome == "probe_failed":
        failing(fake_runtime(client), exit_code=1)
    else:
        failing(fake_runtime(client), ContainerNotRunningError("gone"))

    body = validate(client, token, lab["id"]).json()

    assert body["outcome"] == ("failed" if outcome == "failed" else "error")
    assert (body["newly_completed"], body["progress"]) == (False, NOT_COMPLETED)
    assert completions(client) == []
    assert len(runs(client)) == 1


def test_failed_and_error_runs_keep_an_earlier_completion(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    solve(client, lab)
    progress = validate(client, token, lab["id"]).json()["progress"]
    before = completions(client)

    install(fake_runtime(client), LabState())
    failed = validate(client, token, lab["id"]).json()
    failing(fake_runtime(client), exit_code=1)
    error = validate(client, token, lab["id"]).json()

    assert (failed["outcome"], error["outcome"]) == ("failed", "error")
    for body in (failed, error):
        assert (body["newly_completed"], body["progress"]) == (False, progress)
    assert completions(client) == before


def test_an_answer_mission_is_completed_by_its_answer(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = create_lab(client, token, "sample-answer").json()
    install(fake_runtime(client), LabState())
    word = lab_params(client, lab["id"])["word"]

    wrong = validate(client, token, lab["id"], answer="definitely-not-it").json()
    right = validate(client, token, lab["id"], answer=word).json()

    assert (wrong["newly_completed"], wrong["progress"]) == (False, NOT_COMPLETED)
    assert (right["outcome"], right["newly_completed"]) == ("passed", True)
    assert [row[:3] for row in completions(client)] == [("ana@example.com", "sample-answer", 1)]


def test_a_lab_on_an_older_version_completes_that_version(
    client: TestClient, tmp_path: Path
) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    root = copy_fixture(tmp_path)

    def stricter(data: dict[str, Any]) -> None:
        data["validation"]["all"].append(
            {"id": "extra", "type": "path_absent", "path": SCRIPT, "fail_message": "Remova."}
        )

    edit_yaml(mission_file(root, MISSION), stricter)
    sync(client, root)
    assert sql(client, "SELECT current_version FROM missions WHERE slug = :s", s=MISSION) == [(2,)]
    solve(client, lab)

    body = validate(client, token, lab["id"]).json()

    # Checked and completed against the lab's version 1, which version 2 would fail.
    assert (body["outcome"], body["newly_completed"]) == ("passed", True)
    assert body["mission"]["version"] == 1
    assert body["progress"]["completed_version"] == 1
    assert [row[:3] for row in completions(client)] == [("ana@example.com", MISSION, 1)]


def test_a_mission_archived_after_the_lab_started_is_still_completed(
    client: TestClient, tmp_path: Path
) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    root = copy_fixture(tmp_path)
    edit_yaml(module_file(root, "alpha"), lambda data: data["missions"].remove(MISSION))
    shutil.rmtree(root / "missions" / MISSION)
    sync(client, root)
    assert sql(client, "SELECT status FROM missions WHERE slug = :s", s=MISSION) == [("archived",)]
    solve(client, lab)

    body = validate(client, token, lab["id"]).json()

    assert (body["outcome"], body["newly_completed"]) == ("passed", True)
    assert [row[:3] for row in completions(client)] == [("ana@example.com", MISSION, 1)]


def test_another_users_lab_records_nothing_for_anyone(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, _, lab = setup
    bia = user(client, "bia@example.com")
    solve(client, lab)

    response = validate(client, bia, lab["id"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "lab_not_found"
    assert runs(client) == []
    assert completions(client) == []


def test_progress_outlives_the_lab(setup: tuple[TestClient, str, dict[str, Any]]) -> None:
    client, token, lab = setup
    solve(client, lab)
    first = validate(client, token, lab["id"]).json()

    assert end_lab(client, token, lab["id"]).json()["status"] == "terminated"
    assert len(completions(client)) == 1

    # A new lab for the same mission: passing again is not a new completion.
    second_lab = ready_lab(client, token)
    solve(client, second_lab)
    again = validate(client, token, second_lab["id"]).json()
    assert (again["newly_completed"], again["progress"]) == (False, first["progress"])
    assert len(completions(client)) == 1


# Concurrency: another transaction inserts the same user's completion of the same
# mission and keeps it uncommitted. The validation's insert then waits on the row's
# lock in PostgreSQL; the test waits for that, not for a delay, then ends the other
# transaction. Labs and the in-memory lock play no part.


async def lock_waiters(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count: int = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )
    return count


async def until_waiting_on_a_lock(engine: AsyncEngine) -> None:
    async with asyncio.timeout(10):
        while not await lock_waiters(engine):
            await asyncio.sleep(0.01)


def race(client: TestClient, lab: dict[str, Any], *, other: str) -> Run | None:
    """Validate the lab while another transaction holds the same completion, which
    it then commits or rolls back (`other`). With `cancel`, the validation is
    cancelled while it waits, then the other transaction rolls back; None."""
    engine: AsyncEngine = client.app.state.engine  # type: ignore[attr-defined]
    validations: Validations = client.app.state.validations  # type: ignore[attr-defined]
    owner = user_id(client, "ana@example.com")
    mission = mission_id(client)

    async def scenario() -> Run | None:
        async with engine.connect() as blocker:
            await blocker.execute(
                text(
                    "INSERT INTO user_missions (user_id, mission_id, completed_version,"
                    " completed_at) VALUES (:user, :mission, 1, :at)"
                ),
                {"user": owner, "mission": mission, "at": OTHER_COMPLETION},
            )
            task = asyncio.create_task(validations.validate(uuid.UUID(lab["id"]), None))
            await until_waiting_on_a_lock(engine)
            assert not task.done()
            if other == "cancel":
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    await blocker.rollback()
                    return None
                raise AssertionError("the validation was not cancelled")
            if other == "commit":
                await blocker.commit()
            else:
                await blocker.rollback()
            return await task

    result: Run | None = run_async(client, scenario)
    return result


def test_a_completion_committed_first_by_another_transaction_wins(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, _, lab = setup
    solve(client, lab)

    result = race(client, lab, other="commit")

    assert result is not None
    assert result.outcome == "passed"
    assert result.newly_completed is False
    # The stored completion, not this run's.
    assert result.progress.model_dump() == completed(1, OTHER_COMPLETION)
    assert completions(client) == [("ana@example.com", MISSION, 1, OTHER_COMPLETION)]
    assert [row[4] for row in runs(client)] == ["passed"]


def test_a_completion_rolled_back_by_another_transaction_is_recorded_by_the_run(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, _, lab = setup
    solve(client, lab)

    result = race(client, lab, other="rollback")

    assert result is not None
    assert result.newly_completed is True
    finished_at = runs(client)[0][9]
    assert result.progress.model_dump() == completed(1, finished_at)
    assert completions(client) == [("ana@example.com", MISSION, 1, finished_at)]


def test_a_validation_cancelled_while_recording_stores_nothing(
    setup: tuple[TestClient, str, dict[str, Any]],
) -> None:
    client, token, lab = setup
    solve(client, lab)

    assert race(client, lab, other="cancel") is None

    assert runs(client) == []
    assert completions(client) == []
    # The lab is released and the next validation records the completion.
    assert validate(client, token, lab["id"]).json()["newly_completed"] is True


# A failed write rolls back the whole transaction: the run and the completion are
# stored together or not at all.

FAILURE = "synthetic progress write failure"


@pytest.fixture
def failing_insert(client: TestClient) -> Iterator[Any]:
    """Make every INSERT into the given table fail inside PostgreSQL."""
    tables: list[str] = []

    def fail(table: str) -> None:
        sql(
            client,
            f"CREATE FUNCTION test_fail_{table}() RETURNS trigger LANGUAGE plpgsql AS $$"
            f" BEGIN RAISE EXCEPTION '{FAILURE}'; END $$",
        )
        sql(
            client,
            f"CREATE TRIGGER test_fail_{table} BEFORE INSERT ON {table}"
            f" FOR EACH ROW EXECUTE FUNCTION test_fail_{table}()",
        )
        tables.append(table)

    yield fail
    for table in tables:
        sql(client, f"DROP TRIGGER test_fail_{table} ON {table}")
        sql(client, f"DROP FUNCTION test_fail_{table}()")


@pytest.mark.parametrize("table", ["user_missions", "validation_runs"])
def test_a_failed_write_stores_neither_the_run_nor_the_completion(
    setup: tuple[TestClient, str, dict[str, Any]], failing_insert: Any, table: str
) -> None:
    client, token, lab = setup
    solve(client, lab)
    failing_insert(table)

    # The test client re-raises what the server answers as 500 internal_error.
    with pytest.raises(DBAPIError, match=FAILURE):
        validate(client, token, lab["id"])

    assert runs(client) == []
    assert completions(client) == []
