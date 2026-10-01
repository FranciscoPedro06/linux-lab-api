"""Lab endpoints against PostgreSQL, with labs on a FakeRuntime."""

import uuid

import pytest
from fastapi.testclient import TestClient

from linuxlab.labs.runtime.spec import container_name
from tests.auth.support import ORIGIN, request, sql
from tests.conftest import AppFactory

from .lab_support import (
    LAB_FIELDS,
    container,
    create_lab,
    current_lab,
    end_lab,
    fake_runtime,
    get_lab,
    hold,
    in_background,
    lab_row,
    ready_lab,
    user,
    wait_for_call,
)

pytestmark = pytest.mark.integration


# Creation


def test_create_starts_a_container_for_the_user(make_client: AppFactory) -> None:
    client = make_client(lab_deployment="api-tests")
    ana = user(client, "ana@example.com")

    response = create_lab(client, ana)

    assert response.status_code == 201
    lab = response.json()
    assert set(lab) == LAB_FIELDS
    assert lab["status"] == "ready"
    assert lab["end_reason"] is None
    assert lab["ended_at"] is None
    info = container(client, lab["id"])
    assert info is not None
    assert info.running
    assert info.labels == {
        "linuxlab.managed": "true",
        "linuxlab.lab_id": uuid.UUID(lab["id"]).hex,
        "linuxlab.deployment": "api-tests",
    }
    owner = sql(client, "SELECT u.email FROM lab_sessions l JOIN users u ON u.id = l.user_id")
    assert owner == [("ana@example.com",)]


def test_lab_lifetime_is_two_hours(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))

    rows = sql(
        client,
        "SELECT expires_at - created_at, last_activity_at >= created_at FROM lab_sessions",
    )
    assert rows[0][0].total_seconds() == 2 * 60 * 60
    assert rows[0][1] is True
    assert lab["expires_at"] > lab["created_at"]


def test_create_returns_the_ready_lab_the_user_already_has(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    again = create_lab(client, ana)

    assert again.status_code == 200
    assert again.json() == first
    assert len(fake_runtime(client).containers()) == 1


def test_create_takes_an_empty_json_body(client: TestClient) -> None:
    ana = user(client, "ana@example.com")

    extra = request(client, "POST", "/api/labs", token=ana, json={"user_id": str(uuid.uuid4())})
    no_origin = request(client, "POST", "/api/labs", token=ana, json={}, headers={"origin": ""})
    form = request(client, "POST", "/api/labs", token=ana, content=b"", headers={"origin": ORIGIN})

    assert extra.status_code == 422
    assert no_origin.status_code == 403
    assert form.status_code == 415
    assert fake_runtime(client).containers() == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/labs"),
        ("GET", "/api/labs/current"),
        ("GET", f"/api/labs/{uuid.uuid4()}"),
        ("POST", "/api/labs"),
        ("DELETE", f"/api/labs/{uuid.uuid4()}"),
    ],
)
def test_lab_routes_require_a_session(client: TestClient, method: str, path: str) -> None:
    kwargs: dict[str, object] = {} if method == "GET" else {"json": {}}

    response = request(client, method, path, token="not-a-session", **kwargs)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert fake_runtime(client).containers() == []


def test_failed_provisioning_ends_the_lab_and_removes_the_container(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    fake_runtime(client).fail.add("start")

    response = create_lab(client, ana)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_start_failed"
    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("terminated", "provisioning_failed")
    ]
    assert fake_runtime(client).containers() == []
    assert current_lab(client, ana) is None

    fake_runtime(client).fail.clear()
    assert create_lab(client, ana).status_code == 201


def test_unreachable_docker_leaves_the_failed_lab_for_the_reaper(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    fake_runtime(client).available = False

    response = create_lab(client, ana)

    assert response.status_code == 503
    # The container may exist; only a confirmed removal makes the lab terminated.
    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("failed", "provisioning_failed")
    ]
    # A failed lab is not active: the user can try again at once.
    fake_runtime(client).available = True
    assert create_lab(client, ana).status_code == 201


def test_global_capacity_is_enforced(make_client: AppFactory) -> None:
    client = make_client(lab_capacity=1)
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    ready_lab(client, ana)

    response = create_lab(client, bia)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_capacity_reached"
    assert len(fake_runtime(client).containers()) == 1
    # The user who has a lab still gets it back.
    assert create_lab(client, ana).status_code == 200


def test_creation_is_rate_limited_per_user(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    for _ in range(10):
        assert create_lab(client, ana).status_code in (200, 201)

    limited = create_lab(client, ana)

    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0
    assert create_lab(client, bia).status_code == 201


# Concurrency


def test_concurrent_creations_by_one_user_make_one_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    release = hold(client, "create")

    first = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "create")
    second = create_lab(client, ana)
    assert current_lab(client, ana)["status"] == "provisioning"
    release()

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "lab_provisioning"
    assert first.result(10).status_code == 201
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(1,)]
    assert len(fake_runtime(client).containers()) == 1


def test_ending_a_lab_while_it_is_provisioning(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    release = hold(client, "start")

    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "start")
    lab_id = current_lab(client, ana)["id"]
    ended = end_lab(client, ana, lab_id)
    release()
    created = creating.result(10)

    assert ended.status_code == 200
    assert created.status_code == 201
    # The lab never becomes ready once it has ended, and no container is left behind.
    assert created.json()["status"] == "terminated"
    assert lab_row(client, lab_id) == ("terminated", "user")
    assert fake_runtime(client).containers() == []


def test_creation_waits_for_the_previous_lab_to_finish_ending(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    release = hold(client, "remove")

    ending = in_background(lambda: end_lab(client, ana, lab["id"]))
    wait_for_call(client, "remove")
    blocked = create_lab(client, ana)
    release()

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "lab_terminating"
    assert ending.result(10).json()["status"] == "terminated"
    assert create_lab(client, ana).status_code == 201


def test_concurrent_deletes_both_succeed(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    release = hold(client, "remove")

    first = in_background(lambda: end_lab(client, ana, lab["id"]))
    wait_for_call(client, "remove")
    second = in_background(lambda: end_lab(client, ana, lab["id"]))
    release()

    assert first.result(10).status_code == 200
    assert second.result(10).status_code == 200
    assert lab_row(client, lab["id"]) == ("terminated", "user")
    assert fake_runtime(client).containers() == []


# Reading


def test_current_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    assert current_lab(client, ana) is None

    lab = ready_lab(client, ana)
    assert current_lab(client, ana) == lab

    end_lab(client, ana, lab["id"])
    assert current_lab(client, ana) is None


def test_list_shows_only_the_users_labs_newest_first(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    first = ready_lab(client, ana)
    end_lab(client, ana, first["id"])
    second = ready_lab(client, ana)
    ready_lab(client, bia)

    response = request(client, "GET", "/api/labs", token=ana)

    assert response.status_code == 200
    assert [lab["id"] for lab in response.json()] == [second["id"], first["id"]]
    assert all(set(lab) == LAB_FIELDS for lab in response.json())


def test_get_returns_the_users_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    response = get_lab(client, ana, lab["id"])

    assert response.status_code == 200
    assert response.json() == lab


# Ownership


def test_another_users_lab_looks_like_a_missing_one(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab = ready_lab(client, ana)

    foreign = get_lab(client, bia, lab["id"])
    missing = get_lab(client, bia, str(uuid.uuid4()))

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.json()["error"]["code"] == "lab_not_found"


def test_a_user_cannot_end_another_users_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab_a = ready_lab(client, ana)
    lab_b = ready_lab(client, bia)

    response = end_lab(client, bia, lab_a["id"])

    assert response.status_code == 404
    assert lab_row(client, lab_a["id"]) == ("ready", None)
    info = container(client, lab_a["id"])
    assert info is not None and info.running
    assert lab_row(client, lab_b["id"]) == ("ready", None)


@pytest.mark.parametrize(
    "lab_id",
    [
        "not-a-uuid",
        "0" * 32,
        "{00000000-0000-0000-0000-000000000000}",
        "00000000-0000-0000-0000-00000000000G",
        "%27%20OR%201=1",
    ],
)
def test_malformed_lab_ids_are_not_found(client: TestClient, lab_id: str) -> None:
    ana = user(client, "ana@example.com")
    ready_lab(client, ana)

    assert get_lab(client, ana, lab_id).status_code == 404
    assert end_lab(client, ana, lab_id).status_code == 404


def test_the_hex_form_of_an_own_lab_id_is_not_accepted(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    assert get_lab(client, ana, uuid.UUID(lab["id"]).hex).status_code == 404


# Ending


def test_delete_ends_the_lab_and_removes_its_container(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    response = end_lab(client, ana, lab["id"])

    assert response.status_code == 200
    ended = response.json()
    assert ended["status"] == "terminated"
    assert ended["end_reason"] == "user"
    assert ended["ended_at"] is not None
    assert container(client, lab["id"]) is None


def test_delete_is_idempotent_and_keeps_the_first_reason(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    end_lab(client, ana, lab["id"])

    again = end_lab(client, ana, lab["id"])

    assert again.status_code == 200
    assert again.json()["status"] == "terminated"
    assert again.json()["end_reason"] == "user"


def test_failed_removal_leaves_the_lab_terminating(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")

    response = end_lab(client, ana, lab["id"])

    assert response.status_code == 200
    assert response.json()["status"] == "terminating"
    # Still the active lab: no new one until this one is gone.
    assert current_lab(client, ana)["status"] == "terminating"
    assert create_lab(client, ana).status_code == 409


def test_container_name_does_not_grant_access(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    name = container_name(uuid.UUID(lab["id"]).hex)

    assert get_lab(client, ana, name).status_code == 404
