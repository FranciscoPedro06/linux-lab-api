"""GET /api/labs/{id}/history against PostgreSQL and a FakeRuntime."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.auth.support import login, logout, request, session_token, sql
from tests.labs.lab_support import end_lab, ready_lab, user

pytestmark = pytest.mark.integration


def history(client: TestClient, token: str | None, lab_id: str, **params: Any) -> Response:
    return request(client, "GET", f"/api/labs/{lab_id}/history", token=token, params=params)


def add(client: TestClient, lab_id: str, *commands: str) -> None:
    for command in commands:
        sql(
            client,
            "INSERT INTO command_history (lab_session_id, seq, command, recorded_at)"
            " SELECT :lab, COALESCE(MAX(seq), 0) + 1, :command, '2026-10-25T12:00:00Z'"
            " FROM command_history WHERE lab_session_id = :lab",
            lab=lab_id,
            command=command,
        )


def replace(client: TestClient, token: str, lab_id: str, mission: str) -> dict[str, Any]:
    response = request(
        client, "POST", f"/api/labs/{lab_id}/replace", token=token, json={"mission_slug": mission}
    )
    assert response.status_code == 201, response.text
    replaced: dict[str, Any] = response.json()
    return replaced


def test_an_empty_history(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)

    response = history(client, token, lab["id"])

    assert response.status_code == 200
    assert response.json() == {"entries": [], "more": False}


def test_entries_in_seq_order_with_only_public_fields(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    add(client, lab["id"], "ls -la", "cat <<EOF\nbody\nEOF", "ls -la")

    body = history(client, token, lab["id"]).json()

    assert body == {
        "entries": [
            {"seq": 1, "command": "ls -la", "recorded_at": "2026-10-25T12:00:00Z"},
            {"seq": 2, "command": "cat <<EOF\nbody\nEOF", "recorded_at": "2026-10-25T12:00:00Z"},
            {"seq": 3, "command": "ls -la", "recorded_at": "2026-10-25T12:00:00Z"},
        ],
        "more": False,
    }


def test_pages_with_after_and_limit(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    add(client, lab["id"], *(f"echo {index}" for index in range(5)))

    first = history(client, token, lab["id"], limit=2).json()
    second = history(client, token, lab["id"], after=2, limit=2).json()
    last = history(client, token, lab["id"], after=4, limit=2).json()
    beyond = history(client, token, lab["id"], after=5).json()

    assert [entry["seq"] for entry in first["entries"]] == [1, 2] and first["more"]
    assert [entry["seq"] for entry in second["entries"]] == [3, 4] and second["more"]
    assert [entry["seq"] for entry in last["entries"]] == [5] and not last["more"]
    assert beyond == {"entries": [], "more": False}


def test_the_default_page_is_200_entries(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    sql(
        client,
        "INSERT INTO command_history (lab_session_id, seq, command, recorded_at)"
        " SELECT :lab, n, 'echo ' || n, now() FROM generate_series(1, 501) AS n",
        lab=lab["id"],
    )

    default = history(client, token, lab["id"]).json()
    largest = history(client, token, lab["id"], limit=500).json()

    assert len(default["entries"]) == 200 and default["more"]
    assert len(largest["entries"]) == 500 and largest["more"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"after": -1},
        {"after": 2**31},
        {"after": "x"},
        {"limit": "1.5"},
    ],
)
def test_out_of_range_parameters_are_refused(client: TestClient, params: dict[str, Any]) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)

    response = history(client, token, lab["id"], **params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_requires_a_session(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)

    assert history(client, None, lab["id"]).status_code == 401
    assert history(client, "not-a-session", lab["id"]).status_code == 401


def test_another_users_lab_looks_like_a_missing_one(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab = ready_lab(client, ana)
    add(client, lab["id"], "secret command")

    other = history(client, bia, lab["id"])
    missing = history(client, bia, "00000000-0000-4000-8000-000000000000")
    malformed = history(client, bia, "not-a-lab")

    for response in (other, missing, malformed):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "lab_not_found"
    assert "secret" not in other.text


def test_an_ended_lab_keeps_its_history(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    add(client, lab["id"], "ls")
    assert end_lab(client, token, lab["id"]).status_code == 200

    body = history(client, token, lab["id"]).json()

    assert [entry["command"] for entry in body["entries"]] == ["ls"]


@pytest.mark.parametrize("mission", ["sample-file", "sample-answer"])
def test_a_successor_starts_empty_and_the_replaced_lab_keeps_its_history(
    client: TestClient, mission: str
) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    add(client, lab["id"], "ls")

    successor = replace(client, token, lab["id"], mission)

    assert history(client, token, successor["id"]).json() == {"entries": [], "more": False}
    assert [e["command"] for e in history(client, token, lab["id"]).json()["entries"]] == ["ls"]


def test_logging_out_ends_the_lab_but_keeps_its_history(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)
    add(client, lab["id"], "ls")
    assert logout(client, token).status_code == 204

    token = session_token(login(client, "ana@example.com"))
    body = history(client, token, lab["id"]).json()
    assert [entry["command"] for entry in body["entries"]] == ["ls"]


def test_the_history_cannot_be_written_through_the_api(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    lab = ready_lab(client, token)

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = request(
            client, method, f"/api/labs/{lab['id']}/history", token=token, json={"command": "x"}
        )
        assert response.status_code == 405
    assert sql(client, "SELECT count(*) FROM command_history") == [(0,)]
