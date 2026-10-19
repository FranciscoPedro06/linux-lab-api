"""Replacing a lab: reset (same mission) and mission switch.

PostgreSQL with labs on a FakeRuntime. Concurrent requests are ordered with runtime
gates (see lab_support.hold), never with sleeps.
"""

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.exc import DBAPIError

from linuxlab.labs.lifecycle import Labs, Target
from linuxlab.labs.models import ACTIVE_STATUSES_SQL, LabSession
from linuxlab.labs.runtime.fake import Operation
from linuxlab.main import create_app
from tests.auth.support import ORIGIN, login, logout, request, session_token, sql
from tests.conftest import AppFactory, app_settings
from tests.content.support import copy_fixture, edit_yaml, module_file

from .lab_support import (
    MISSION,
    container,
    create_lab,
    current_lab,
    end_lab,
    fake_runtime,
    get_lab,
    hold,
    in_background,
    lab_row,
    labs,
    publish_missions,
    ready_lab,
    run_async,
    user,
    wait_for_call,
)
from .test_lab_versions import change_briefing, pinned, sync

pytestmark = pytest.mark.integration

OTHER = "sample-answer"


def replace_lab(
    client: TestClient, token: str | None, lab_id: str, mission: str = MISSION
) -> Response:
    return request(
        client, "POST", f"/api/labs/{lab_id}/replace", token=token, json={"mission_slug": mission}
    )


def successors(client: TestClient, lab_id: str) -> list[tuple[str, str, str | None]]:
    rows = sql(
        client,
        "SELECT id::text, status, end_reason FROM lab_sessions WHERE previous_lab_id = :id",
        id=lab_id,
    )
    return [tuple(row) for row in rows]


def user_id(client: TestClient, email: str) -> uuid.UUID:
    rows = sql(client, "SELECT id FROM users WHERE email = :email", email=email)
    return uuid.UUID(str(rows[0][0]))


def wait_for_calls(client: TestClient, operation: Operation, count: int) -> None:
    deadline = time.monotonic() + 5
    while sum(name == operation for name, _ in fake_runtime(client).calls) < count:
        assert time.monotonic() < deadline, f"runtime called {operation} fewer than {count} times"
        time.sleep(0.01)


def assert_invariants(client: TestClient) -> None:
    """At most one active lab per user and one successor per lab, whatever happened."""
    assert (
        sql(
            client,
            "SELECT user_id FROM lab_sessions"
            f" WHERE status IN ({ACTIVE_STATUSES_SQL}) GROUP BY user_id HAVING count(*) > 1",
        )
        == []
    )
    assert (
        sql(
            client,
            "SELECT previous_lab_id FROM lab_sessions WHERE previous_lab_id IS NOT NULL"
            " GROUP BY previous_lab_id HAVING count(*) > 1",
        )
        == []
    )


def lab_count(client: TestClient) -> int:
    count: int = sql(client, "SELECT count(*) FROM lab_sessions")[0][0]
    return count


def patch_before_successor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, action: Any
) -> None:
    """Run `action` once the lab being replaced is removed, before its successor is
    created: the gap in which other actors may change what the replacement finds."""
    service = labs(client)
    original = service._create_successor

    async def first_the_action(source: LabSession, target: Target) -> Any:
        await action(service)
        return await original(source, target)

    monkeypatch.setattr(service, "_create_successor", first_the_action)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


# Reset and switch


def test_reset_creates_a_new_lab_for_the_same_mission(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    _, _, first_params = pinned(client, first["id"])

    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 201, response.text
    second = response.json()
    assert second["id"] != first["id"]
    assert second["status"] == "ready"
    assert second["mission"] == first["mission"]
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert get_lab(client, ana, first["id"]).json()["end_reason"] == "reset"
    assert container(client, first["id"]) is None
    info = container(client, second["id"])
    assert info is not None and info.running
    assert info.labels["linuxlab.lab_id"] == uuid.UUID(second["id"]).hex
    assert successors(client, first["id"]) == [(second["id"], "ready", None)]
    slug, _, second_params = pinned(client, second["id"])
    assert slug == MISSION
    assert set(second_params) == set(first_params)
    assert second_params != first_params
    assert current_lab(client, ana)["id"] == second["id"]
    assert "previous_lab_id" not in second
    assert_invariants(client)


def test_switch_creates_a_lab_for_the_other_mission(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    response = replace_lab(client, ana, first["id"], OTHER)

    assert response.status_code == 201, response.text
    second = response.json()
    assert second["mission"]["slug"] == OTHER
    current = sql(client, "SELECT current_version FROM missions WHERE slug = :slug", slug=OTHER)[0][
        0
    ]
    assert second["mission"]["version"] == current
    assert lab_row(client, first["id"]) == ("terminated", "mission_switch")
    assert successors(client, first["id"]) == [(second["id"], "ready", None)]
    assert_invariants(client)


def test_reset_uses_the_current_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    change_briefing(root)
    sync(client, root)

    second = replace_lab(client, ana, first["id"]).json()

    assert pinned(client, first["id"])[1] == 1
    assert pinned(client, second["id"])[1] == 2
    assert second["mission"]["version"] == 2


def test_the_version_selected_before_ending_the_lab_is_the_one_created(
    client: TestClient, root: Path
) -> None:
    sync(client, root)
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "remove")
    pending = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_call(client, "remove")

    # Published after the replacement checked the mission, before it created the lab.
    change_briefing(root)
    sync(client, root)
    release()

    second = pending.result(timeout=10).json()
    assert second["mission"]["version"] == 1


def test_a_lab_without_a_mission_is_switched(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    legacy = uuid.uuid4()
    sql(
        client,
        "INSERT INTO lab_sessions (id, user_id, status, created_at, last_activity_at, expires_at)"
        " SELECT :id, id, 'ready', now(), now(), now() + interval '2 hours' FROM users",
        id=legacy,
    )

    response = replace_lab(client, ana, str(legacy))

    assert response.status_code == 201, response.text
    assert lab_row(client, str(legacy)) == ("terminated", "mission_switch")


def test_progress_and_validation_runs_are_kept(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    sql(
        client,
        "INSERT INTO validation_runs (lab_session_id, mission_id, mission_version, outcome,"
        " result, answer_provided, started_at, finished_at)"
        " SELECT id, mission_id, mission_version, 'passed', '{}', false, now(), now()"
        " FROM lab_sessions WHERE id = :id",
        id=first["id"],
    )
    sql(
        client,
        "INSERT INTO user_missions (user_id, mission_id, completed_version, completed_at)"
        " SELECT user_id, mission_id, mission_version, now() FROM lab_sessions WHERE id = :id",
        id=first["id"],
    )
    before = sql(client, "SELECT * FROM user_missions")

    replace_lab(client, ana, first["id"])
    second = replace_lab(client, ana, first["id"]).json()
    replace_lab(client, ana, second["id"], OTHER)

    assert sql(client, "SELECT * FROM user_missions") == before
    assert sql(client, "SELECT lab_session_id::text, outcome FROM validation_runs") == [
        (first["id"], "passed")
    ]
    detail = request(client, "GET", f"/api/missions/{MISSION}", token=ana).json()
    assert detail["progress"]["status"] == "completed"


# The request


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mission_slug": None},
        {"mission_slug": 1},
        {"mission_slug": MISSION, "version": 1},
        {"mission_slug": MISSION, "params": {"token": "00"}},
        {"mission_slug": MISSION, "previous_lab_id": str(uuid.uuid4())},
        {"mission_slug": MISSION, "user_id": str(uuid.uuid4())},
    ],
)
def test_replace_takes_only_a_mission_slug(client: TestClient, body: dict[str, object]) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    response = request(client, "POST", f"/api/labs/{first['id']}/replace", token=ana, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert lab_row(client, first["id"]) == ("ready", None)


def test_replace_requires_a_session_and_an_allowed_origin(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    anonymous = replace_lab(client, None, first["id"])
    no_origin = request(
        client,
        "POST",
        f"/api/labs/{first['id']}/replace",
        token=ana,
        json={"mission_slug": MISSION},
        headers={"origin": ""},
    )

    assert anonymous.status_code == 401
    assert no_origin.status_code == 403
    assert lab_row(client, first["id"]) == ("ready", None)


def test_another_users_lab_looks_like_a_missing_one(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab = ready_lab(client, ana)

    other = replace_lab(client, bia, lab["id"])
    missing = replace_lab(client, bia, str(uuid.uuid4()))
    malformed = replace_lab(client, bia, uuid.UUID(lab["id"]).hex)

    for response in (other, missing, malformed):
        assert response.status_code == 404
        assert response.json() == other.json()
    assert other.json()["error"]["code"] == "lab_not_found"
    assert lab_row(client, lab["id"]) == ("ready", None)
    assert current_lab(client, bia) is None
    assert lab_count(client) == 1


@pytest.mark.parametrize("slug", ["sample-draft", "no-such-mission", "Not A Slug"])
def test_an_unavailable_mission_leaves_the_lab_alone(client: TestClient, slug: str) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    response = replace_lab(client, ana, lab["id"], slug)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mission_not_found"
    assert lab_row(client, lab["id"]) == ("ready", None)
    info = container(client, lab["id"])
    assert info is not None and info.running
    assert lab_count(client) == 1


def test_an_archived_mission_cannot_be_reset_into(client: TestClient, root: Path) -> None:
    sync(client, root)
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    edit_yaml(module_file(root, "alpha"), lambda data: data["missions"].remove(MISSION))
    shutil.rmtree(root / "missions" / MISSION)
    sync(client, root)

    response = replace_lab(client, ana, lab["id"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mission_not_found"
    assert lab_row(client, lab["id"]) == ("ready", None)


def test_an_unusable_version_leaves_the_lab_alone(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    mission_id, spec = sql(
        client,
        "SELECT v.mission_id, v.spec FROM mission_versions v"
        " JOIN missions m ON m.id = v.mission_id WHERE m.slug = :slug",
        slug=OTHER,
    )[0]
    spec["environment"]["image"] = "unknown-alias"
    sql(
        client,
        "INSERT INTO mission_versions (mission_id, version, content_hash, spec)"
        " VALUES (:id, 2, :hash, CAST(:spec AS jsonb))",
        id=mission_id,
        hash="0" * 64,
        spec=json.dumps(spec),
    )
    sql(client, "UPDATE missions SET current_version = 2 WHERE id = :id", id=mission_id)

    response = replace_lab(client, ana, lab["id"], OTHER)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert lab_row(client, lab["id"]) == ("ready", None)
    assert lab_count(client) == 1


def test_the_replaced_lab_frees_its_own_place(make_client: AppFactory) -> None:
    client = make_client(lab_capacity=1)
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    response = replace_lab(client, ana, lab["id"], OTHER)

    assert response.status_code == 201


def test_full_capacity_leaves_the_lab_alone(make_client: AppFactory) -> None:
    client = make_client(lab_capacity=1)
    ana = user(client, "ana@example.com")
    user(client, "bia@example.com")
    lab = ready_lab(client, ana)
    # Another user's lab, inserted directly as if the cap had been lowered meanwhile.
    sql(
        client,
        "INSERT INTO lab_sessions (id, user_id, status, created_at, last_activity_at, expires_at)"
        " SELECT :id, id, 'ready', now(), now(), now() + interval '2 hours' FROM users"
        " WHERE email = 'bia@example.com'",
        id=uuid.uuid4(),
    )

    response = replace_lab(client, ana, lab["id"])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_capacity_reached"
    assert lab_row(client, lab["id"]) == ("ready", None)
    assert successors(client, lab["id"]) == []


def test_capacity_taken_after_the_check_leaves_no_lab(
    make_client: AppFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(lab_capacity=1)
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab = ready_lab(client, ana)
    bia_id = user_id(client, "bia@example.com")

    async def bia_starts_a_lab(service: Labs) -> None:
        await service.create(bia_id, OTHER)

    patch_before_successor(client, monkeypatch, bia_starts_a_lab)
    response = replace_lab(client, ana, lab["id"])

    # The accepted risk: the lab was ended, and its successor could not be created.
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_capacity_reached"
    assert lab_row(client, lab["id"]) == ("terminated", "reset")
    assert successors(client, lab["id"]) == []
    assert current_lab(client, ana) is None
    assert current_lab(client, bia)["status"] == "ready"
    monkeypatch.undo()

    # Asking again resumes it once a place is free.
    assert replace_lab(client, ana, lab["id"]).json()["error"]["code"] == "lab_capacity_reached"
    end_lab(client, bia, current_lab(client, bia)["id"])
    resumed = replace_lab(client, ana, lab["id"])
    assert resumed.status_code == 201
    assert successors(client, lab["id"]) == [(resumed.json()["id"], "ready", None)]
    assert_invariants(client)


# Repeating a replacement


def test_asking_again_returns_the_same_successor(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    second = replace_lab(client, ana, first["id"]).json()
    # Use up the creation limit: asking again creates nothing, so it is not counted.
    for _ in range(9):
        create_lab(client, ana)
    assert create_lab(client, ana).status_code == 429

    again = replace_lab(client, ana, first["id"])

    assert again.status_code == 200
    assert again.json() == get_lab(client, ana, second["id"]).json()
    assert successors(client, first["id"]) == [(second["id"], "ready", None)]
    assert lab_count(client) == 2
    # A new replacement is counted, and refused.
    assert replace_lab(client, ana, second["id"]).status_code == 429
    assert lab_row(client, second["id"]) == ("ready", None)


def test_asking_for_another_mission_after_the_replacement_is_refused(
    client: TestClient,
) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    second = replace_lab(client, ana, first["id"]).json()

    response = replace_lab(client, ana, first["id"], OTHER)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_current"
    assert lab_row(client, second["id"]) == ("ready", None)
    assert lab_count(client) == 2


def test_a_successor_that_failed_is_never_replaced_by_another(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    fake_runtime(client).fail.add("start")

    failed = replace_lab(client, ana, first["id"])

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "lab_start_failed"
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    [(second_id, status, reason)] = successors(client, first["id"])
    assert (status, reason) == ("terminated", "provisioning_failed")
    assert current_lab(client, ana) is None
    fake_runtime(client).fail.clear()

    # The same request now answers the same way every time, and creates nothing.
    for _ in range(2):
        again = replace_lab(client, ana, first["id"])
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "lab_not_current"
    assert lab_count(client) == 2
    # Starting a new lab is the way forward.
    assert create_lab(client, ana).status_code == 201
    assert second_id


def test_a_successor_being_prepared_is_reported_as_such(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "start")
    pending = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "start", 2)

    same = replace_lab(client, ana, first["id"])
    other = replace_lab(client, ana, first["id"], OTHER)
    release()

    assert same.status_code == 409
    assert same.json()["error"]["code"] == "lab_provisioning"
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "lab_not_current"
    assert pending.result(timeout=10).status_code == 201
    assert len(successors(client, first["id"])) == 1


# Concurrency


def test_concurrent_resets_make_one_successor(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    removal = hold(client, "remove")
    a = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "remove", 1)
    b = in_background(lambda: replace_lab(client, ana, first["id"]))
    # Both wait on the removal. The successor's start is held as well, so the request
    # that does not create it finds it being prepared.
    wait_for_calls(client, "remove", 2)
    start = hold(client, "start")
    removal()
    wait_for_calls(client, "start", 2)
    deadline = time.monotonic() + 10
    while not (a.done() or b.done()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    start()

    results = sorted(
        (response.status_code, response.json().get("error", {}).get("code"))
        for response in (a.result(timeout=10), b.result(timeout=10))
    )
    assert results == [(201, None), (409, "lab_provisioning")]
    assert len(successors(client, first["id"])) == 1
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert len(fake_runtime(client).containers()) == 1
    assert_invariants(client)


@pytest.mark.parametrize(("winner", "loser"), [(MISSION, OTHER), (OTHER, MISSION)])
def test_a_reset_and_a_switch_racing_make_one_successor(
    client: TestClient, winner: str, loser: str
) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    removal = hold(client, "remove")
    a = in_background(lambda: replace_lab(client, ana, first["id"], winner))
    wait_for_calls(client, "remove", 1)

    # The lab already ends for the other kind of replacement.
    b = replace_lab(client, ana, first["id"], loser)
    removal()

    assert b.status_code == 409
    assert b.json()["error"]["code"] == "lab_not_current"
    created = a.result(timeout=10)
    assert created.status_code == 201
    assert created.json()["mission"]["slug"] == winner
    assert len(successors(client, first["id"])) == 1
    assert_invariants(client)


def test_delete_before_the_replacement(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    end_lab(client, ana, first["id"])

    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 201
    assert lab_row(client, first["id"]) == ("terminated", "user")
    assert successors(client, first["id"])[0][1] == "ready"


def test_delete_during_the_replacement(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "remove")
    replacing = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "remove", 1)
    deleting = in_background(lambda: end_lab(client, ana, first["id"]))
    wait_for_calls(client, "remove", 2)
    release()

    assert replacing.result(timeout=10).status_code == 201
    assert deleting.result(timeout=10).json()["status"] == "terminated"
    # The first reason recorded is kept.
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert len(successors(client, first["id"])) == 1
    assert_invariants(client)


def test_reaper_during_the_replacement(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "remove")
    replacing = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "remove", 1)
    reaping = in_background(lambda: run_async(client, lambda: labs(client).reconcile(startup=True)))
    wait_for_calls(client, "remove", 2)
    release()

    assert replacing.result(timeout=10).status_code == 201
    reaping.result(timeout=10)
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert len(successors(client, first["id"])) == 1
    assert len(fake_runtime(client).containers()) == 1
    assert_invariants(client)


def test_a_lab_ended_by_the_reaper_can_still_be_replaced(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    sql(client, "UPDATE lab_sessions SET last_activity_at = now() - interval '1 hour'")
    run_async(client, lambda: labs(client).reconcile())
    assert lab_row(client, first["id"]) == ("terminated", "no_terminal")

    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 201
    assert lab_row(client, first["id"]) == ("terminated", "no_terminal")


def test_a_lab_ended_by_logout_is_not_replaced(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    logout(client, ana)
    again = session_token(login(client, "ana@example.com"))

    response = replace_lab(client, again, first["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_current"
    assert lab_count(client) == 1


def test_logout_while_the_successor_is_prepared_ends_it(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "start")
    replacing = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "start", 2)

    assert logout(client, ana).status_code == 204
    release()

    response = replacing.result(timeout=10)
    assert response.status_code == 201
    assert response.json()["status"] == "terminated"
    assert response.json()["end_reason"] == "logout"
    [(_, status, reason)] = successors(client, first["id"])
    assert (status, reason) == ("terminated", "logout")
    assert fake_runtime(client).containers() == []


def test_a_lab_started_meanwhile_is_not_replaced(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    ana_id = user_id(client, "ana@example.com")

    async def another_tab_starts_a_lab(service: Labs) -> None:
        await service.create(ana_id, OTHER)

    patch_before_successor(client, monkeypatch, another_tab_starts_a_lab)
    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_current"
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert successors(client, first["id"]) == []
    assert current_lab(client, ana)["mission"]["slug"] == OTHER
    assert_invariants(client)


def test_a_lab_that_is_not_the_current_one_is_not_replaced(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    end_lab(client, ana, first["id"])
    second = ready_lab(client, ana)

    response = replace_lab(client, ana, first["id"], OTHER)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_current"
    assert lab_row(client, second["id"]) == ("ready", None)


def test_a_lab_still_provisioning_is_replaced(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    release = hold(client, "exec")
    creating = in_background(lambda: create_lab(client, ana))
    wait_for_calls(client, "exec", 1)
    first = current_lab(client, ana)
    assert first["status"] == "provisioning"

    replacing = in_background(lambda: replace_lab(client, ana, first["id"]))
    # The successor's own preparation reaches the held exec too.
    wait_for_calls(client, "exec", 2)
    release()

    assert replacing.result(timeout=10).status_code == 201
    assert creating.result(timeout=10).json()["status"] == "terminated"
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert container(client, first["id"]) is None
    assert_invariants(client)


# Failures and recovery


def test_failed_removal_is_resumed_by_asking_again(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")

    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "previous_lab_not_removed"
    assert lab_row(client, first["id"]) == ("terminating", "reset")
    assert successors(client, first["id"]) == []
    # Meanwhile the creation limit runs out (the first attempt was counted).
    # Counted so far: the first lab and the replacement.
    for _ in range(8):
        assert create_lab(client, ana).json()["error"]["code"] == "lab_terminating"
    assert create_lab(client, ana).status_code == 429
    assert replace_lab(client, ana, first["id"]).status_code == 503
    fake_runtime(client).fail.clear()

    resumed = replace_lab(client, ana, first["id"])

    assert resumed.status_code == 201
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert container(client, first["id"]) is None
    assert successors(client, first["id"]) == [(resumed.json()["id"], "ready", None)]


def test_restart_after_the_lab_ended_then_resume(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    runtime = fake_runtime(client)
    runtime.fail.add("remove")
    assert replace_lab(client, ana, first["id"]).status_code == 503
    runtime.fail.clear()

    # A new API process on the same database and Docker. Its reaper's startup pass
    # finishes the lab that was left being removed.
    app = create_app(app_settings(), lab_runtime=runtime, start_reaper=True)
    with TestClient(app, base_url="https://testserver", headers={"origin": ORIGIN}) as restarted:
        deadline = time.monotonic() + 10
        while lab_row(restarted, first["id"])[0] != "terminated":
            assert time.monotonic() < deadline, "the startup pass did not finish the lab"
            time.sleep(0.05)
        assert lab_row(restarted, first["id"]) == ("terminated", "reset")
        assert container(restarted, first["id"]) is None

        resumed = replace_lab(restarted, ana, first["id"])

        assert resumed.status_code == 201
        assert successors(restarted, first["id"]) == [(resumed.json()["id"], "ready", None)]


def test_restart_while_the_successor_was_provisioning(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    end_lab(client, ana, first["id"])
    # The state a crash leaves after inserting the successor, before it was ready.
    successor = uuid.uuid4()
    sql(
        client,
        "INSERT INTO lab_sessions (id, user_id, status, created_at, last_activity_at,"
        " expires_at, mission_id, mission_version, params, previous_lab_id)"
        " SELECT :id, user_id, 'provisioning', now() - interval '5 minutes',"
        " now() - interval '5 minutes', now() + interval '1 hour', mission_id,"
        " mission_version, params, id FROM lab_sessions WHERE id = :previous",
        id=successor,
        previous=first["id"],
    )

    assert replace_lab(client, ana, first["id"]).json()["error"]["code"] == "lab_provisioning"
    run_async(client, lambda: labs(client).reconcile(startup=True))
    assert lab_row(client, str(successor)) == ("terminated", "provisioning_timeout")

    again = replace_lab(client, ana, first["id"])
    assert again.json()["error"]["code"] == "lab_not_current"
    assert lab_count(client) == 2
    assert create_lab(client, ana).status_code == 201


def test_database_failure_creating_the_successor_is_resumed(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    sql(
        client,
        "CREATE FUNCTION test_fail_lab_insert() RETURNS trigger LANGUAGE plpgsql AS $$"
        " BEGIN RAISE EXCEPTION 'synthetic lab insert failure'; END $$",
    )
    sql(
        client,
        "CREATE TRIGGER test_fail_lab_insert BEFORE INSERT ON lab_sessions"
        " FOR EACH ROW EXECUTE FUNCTION test_fail_lab_insert()",
    )
    try:
        # The test client re-raises what the server answers as 500 internal_error.
        with pytest.raises(DBAPIError, match="synthetic lab insert failure"):
            replace_lab(client, ana, first["id"])
    finally:
        sql(client, "DROP TRIGGER test_fail_lab_insert ON lab_sessions")
        sql(client, "DROP FUNCTION test_fail_lab_insert()")

    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert successors(client, first["id"]) == []

    resumed = replace_lab(client, ana, first["id"])
    assert resumed.status_code == 201
    # A resumed reset cannot become a switch.
    assert replace_lab(client, ana, first["id"], OTHER).json()["error"]["code"] == (
        "lab_not_current"
    )


def test_an_interrupted_reset_is_not_resumed_as_a_switch(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")
    replace_lab(client, ana, first["id"])
    fake_runtime(client).fail.clear()

    response = replace_lab(client, ana, first["id"], OTHER)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_current"
    assert successors(client, first["id"]) == []
