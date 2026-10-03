"""Lab session rules enforced by the database itself, independent of the API."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError

from linuxlab.labs.models import TRANSITIONS, LabStatus
from tests.auth.support import sql

pytestmark = pytest.mark.integration


def add_user(client: TestClient, email: str = "ana@example.com") -> uuid.UUID:
    user_id = uuid.uuid4()
    sql(
        client,
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (:id, :email, '$argon2id$synthetic', 'Ana')",
        id=user_id,
        email=email,
    )
    return user_id


def add_lab(
    client: TestClient,
    user_id: uuid.UUID,
    status: str = "provisioning",
    end_reason: str | None = None,
    ended: bool = False,
) -> uuid.UUID:
    lab_id = uuid.uuid4()
    sql(
        client,
        "INSERT INTO lab_sessions"
        " (id, user_id, status, end_reason, created_at, last_activity_at, expires_at, ended_at)"
        " VALUES (:id, :user_id, :status, :reason, now(), now(), now() + interval '2 hours',"
        " CASE WHEN :ended THEN now() END)",
        id=lab_id,
        user_id=user_id,
        status=status,
        reason=end_reason,
        ended=ended,
    )
    return lab_id


def set_status(client: TestClient, lab_id: uuid.UUID, status: str) -> None:
    ended = status not in ("provisioning", "ready")
    sql(
        client,
        "UPDATE lab_sessions SET status = :status,"
        " end_reason = CASE WHEN :ended THEN coalesce(end_reason, 'user') END,"
        " ended_at = CASE WHEN :ended THEN coalesce(ended_at, now()) END"
        " WHERE id = :id",
        id=lab_id,
        status=status,
        ended=ended,
    )


def add_lab_in(client: TestClient, user_id: uuid.UUID, status: LabStatus) -> uuid.UUID:
    """A lab that reached `status` through valid transitions."""
    path = {
        LabStatus.PROVISIONING: [],
        LabStatus.READY: ["ready"],
        LabStatus.TERMINATING: ["ready", "terminating"],
        LabStatus.FAILED: ["failed"],
        LabStatus.TERMINATED: ["ready", "terminating", "terminated"],
    }[status]
    lab_id = add_lab(client, user_id)
    for step in path:
        set_status(client, lab_id, step)
    return lab_id


@pytest.mark.parametrize("active", ["provisioning", "ready", "terminating"])
def test_a_user_has_at_most_one_active_lab(client: TestClient, active: str) -> None:
    user_id = add_user(client)
    add_lab_in(client, user_id, LabStatus(active))

    with pytest.raises(IntegrityError, match="one_active_lab_per_user"):
        add_lab(client, user_id)


def test_ended_labs_do_not_count_as_active(client: TestClient) -> None:
    user_id = add_user(client)
    add_lab_in(client, user_id, LabStatus.TERMINATED)
    add_lab_in(client, user_id, LabStatus.FAILED)

    add_lab(client, user_id)


def test_other_users_have_their_own_active_lab(client: TestClient) -> None:
    add_lab(client, add_user(client, "ana@example.com"))
    add_lab(client, add_user(client, "bia@example.com"))


ALL_PAIRS = [(old, new) for old in LabStatus for new in LabStatus if old != new]


@pytest.mark.parametrize(("old", "new"), ALL_PAIRS, ids=[f"{o}-{n}" for o, n in ALL_PAIRS])
def test_only_documented_transitions_are_accepted(
    client: TestClient, old: LabStatus, new: LabStatus
) -> None:
    lab_id = add_lab_in(client, add_user(client), old)

    if new in TRANSITIONS[old]:
        set_status(client, lab_id, new)
        assert sql(client, "SELECT status FROM lab_sessions") == [(new,)]
    else:
        with pytest.raises(DBAPIError, match="invalid lab status transition"):
            set_status(client, lab_id, new)


def test_owner_cannot_change(client: TestClient) -> None:
    lab_id = add_lab(client, add_user(client, "ana@example.com"))
    other = add_user(client, "bia@example.com")

    with pytest.raises(DBAPIError, match="identity cannot change"):
        sql(client, "UPDATE lab_sessions SET user_id = :user WHERE id = :id", user=other, id=lab_id)


def test_end_reason_cannot_change_once_recorded(client: TestClient) -> None:
    lab_id = add_lab_in(client, add_user(client), LabStatus.TERMINATING)

    with pytest.raises(DBAPIError, match="end reason cannot change"):
        sql(client, "UPDATE lab_sessions SET end_reason = 'oom' WHERE id = :id", id=lab_id)


@pytest.mark.parametrize(
    ("status", "end_reason", "ended", "constraint"),
    [
        ("ready", "user", False, "ck_lab_sessions_end_reason_when_ended"),
        ("terminated", None, True, "ck_lab_sessions_end_reason_when_ended"),
        ("ready", None, True, "ck_lab_sessions_ended_at_when_ended"),
        ("terminated", "user", False, "ck_lab_sessions_ended_at_when_ended"),
        ("paused", "user", True, "ck_lab_sessions_status"),
        ("terminated", "bored", True, "ck_lab_sessions_end_reason"),
    ],
)
def test_state_columns_are_consistent(
    client: TestClient, status: str, end_reason: str | None, ended: bool, constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        add_lab(client, add_user(client), status, end_reason, ended)


def test_lab_needs_an_existing_user(client: TestClient) -> None:
    with pytest.raises(IntegrityError, match="fk_lab_sessions_user_id_users"):
        add_lab(client, uuid.uuid4())


def test_deleting_a_user_deletes_their_labs(client: TestClient) -> None:
    user_id = add_user(client)
    add_lab(client, user_id)

    sql(client, "DELETE FROM users WHERE id = :id", id=user_id)

    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]
