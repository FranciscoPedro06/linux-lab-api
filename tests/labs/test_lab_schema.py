"""Lab session rules enforced by the database itself, independent of the API."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError

from linuxlab.labs.models import TRANSITIONS, LabStatus
from tests.auth.support import sql
from tests.labs.lab_support import publish_missions

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


# Mission version and parameters


def published_versions(client: TestClient) -> dict[str, tuple[int, int]]:
    """Sync the synthetic content; slug -> (mission id, current version)."""
    publish_missions(client)
    rows = sql(client, "SELECT slug, id, current_version FROM missions")
    return {slug: (mission_id, version) for slug, mission_id, version in rows}


def add_pinned_lab(
    client: TestClient,
    user_id: uuid.UUID,
    mission_id: int | None,
    version: int | None,
    params: str | None,
) -> uuid.UUID:
    lab_id = uuid.uuid4()
    sql(
        client,
        "INSERT INTO lab_sessions"
        " (id, user_id, status, created_at, last_activity_at, expires_at,"
        " mission_id, mission_version, params)"
        " VALUES (:id, :user_id, 'provisioning', now(), now(), now() + interval '2 hours',"
        " :mission_id, :version, CAST(:params AS jsonb))",
        id=lab_id,
        user_id=user_id,
        mission_id=mission_id,
        version=version,
        params=params,
    )
    return lab_id


def test_a_lab_is_pinned_to_an_existing_version_of_its_mission(client: TestClient) -> None:
    versions = published_versions(client)
    mission_id, version = versions["sample-file"]
    other_id, _ = versions["sample-answer"]
    user_id = add_user(client)

    lab_id = add_pinned_lab(client, user_id, mission_id, version, '{"token": "ab12"}')
    assert sql(
        client,
        "SELECT mission_id, mission_version, params FROM lab_sessions WHERE id = :id",
        id=lab_id,
    ) == [(mission_id, version, {"token": "ab12"})]
    sql(client, "DELETE FROM lab_sessions")

    for pin in ((mission_id, version + 1), (other_id + 1000, 1)):
        with pytest.raises(IntegrityError, match="fk_lab_sessions_mission_id_mission_versions"):
            add_pinned_lab(client, user_id, pin[0], pin[1], "{}")


@pytest.mark.parametrize(
    ("pinned", "params"),
    [((True, False), "{}"), ((False, True), "{}"), ((True, True), None), ((False, False), "{}")],
)
def test_mission_version_and_params_are_set_together(
    client: TestClient, pinned: tuple[bool, bool], params: str | None
) -> None:
    mission_id, version = published_versions(client)["sample-file"]

    with pytest.raises(IntegrityError, match="ck_lab_sessions_mission_pinned"):
        add_pinned_lab(
            client,
            add_user(client),
            mission_id if pinned[0] else None,
            version if pinned[1] else None,
            params,
        )


def test_labs_without_a_mission_have_none_of_the_three(client: TestClient) -> None:
    lab_id = add_lab(client, add_user(client))

    assert sql(
        client,
        "SELECT mission_id, mission_version, params FROM lab_sessions WHERE id = :id",
        id=lab_id,
    ) == [(None, None, None)]


@pytest.mark.parametrize("params", ['["a"]', '"a"', "1", "null"])
def test_params_are_a_json_object(client: TestClient, params: str) -> None:
    mission_id, version = published_versions(client)["sample-file"]

    with pytest.raises(IntegrityError, match="ck_lab_sessions_params_object"):
        add_pinned_lab(client, add_user(client), mission_id, version, params)


@pytest.mark.parametrize(
    "change",
    [
        "mission_id = (SELECT id FROM missions WHERE slug = 'sample-answer')",
        "mission_version = mission_version + 1",
        "mission_id = NULL, mission_version = NULL, params = NULL",
        'params = \'{"token": "ffff"}\'',
        'params = params || \'{"extra": "x"}\'',
    ],
)
def test_mission_version_and_params_never_change(client: TestClient, change: str) -> None:
    versions = published_versions(client)
    mission_id, version = versions["sample-file"]
    lab_id = add_pinned_lab(client, add_user(client), mission_id, version, '{"token": "ab12"}')

    with pytest.raises(DBAPIError, match="lab session mission cannot change"):
        sql(client, f"UPDATE lab_sessions SET {change} WHERE id = :id", id=lab_id)

    # The lifecycle itself still moves the lab.
    set_status(client, lab_id, "ready")
    assert sql(client, "SELECT status, params FROM lab_sessions WHERE id = :id", id=lab_id) == [
        ("ready", {"token": "ab12"})
    ]


def test_a_mission_with_labs_cannot_lose_its_versions(client: TestClient) -> None:
    mission_id, version = published_versions(client)["sample-file"]
    add_pinned_lab(client, add_user(client), mission_id, version, "{}")

    with pytest.raises(DBAPIError):
        sql(client, "DELETE FROM mission_versions WHERE mission_id = :id", id=mission_id)
