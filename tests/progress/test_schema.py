"""user_missions rules enforced by the database itself, independent of the API."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.auth.support import sql
from tests.labs.lab_support import publish_missions
from tests.labs.test_lab_schema import add_user

pytestmark = pytest.mark.integration

INSERT = (
    "INSERT INTO user_missions (user_id, mission_id, completed_version, completed_at)"
    " VALUES (:user, :mission, :version, now())"
)


def mission_id(client: TestClient, slug: str) -> int:
    rows = sql(client, "SELECT id FROM missions WHERE slug = :slug", slug=slug)
    assert len(rows) == 1
    value: int = rows[0][0]
    return value


def complete(client: TestClient, user: uuid.UUID, mission: int, version: int = 1) -> None:
    sql(client, INSERT, user=user, mission=mission, version=version)


def progress(client: TestClient) -> list[Any]:
    return sql(
        client,
        "SELECT user_id, mission_id, completed_version FROM user_missions"
        " ORDER BY user_id, mission_id",
    )


@pytest.fixture
def missions(client: TestClient) -> tuple[int, int]:
    publish_missions(client)
    return mission_id(client, "sample-file"), mission_id(client, "sample-answer")


def test_one_completion_per_user_and_mission(client: TestClient, missions: tuple[int, int]) -> None:
    file, answer = missions
    ana, bia = add_user(client), add_user(client, "bia@example.com")
    complete(client, ana, file)
    complete(client, ana, answer)
    complete(client, bia, file)

    with pytest.raises(IntegrityError, match="pk_user_missions"):
        complete(client, ana, file)
    assert len(progress(client)) == 3


def test_the_completed_version_exists_and_belongs_to_the_mission(
    client: TestClient, missions: tuple[int, int]
) -> None:
    file, answer = missions
    ana = add_user(client)
    # Version 2 exists, but only for the other mission.
    sql(
        client,
        "INSERT INTO mission_versions (mission_id, version, content_hash, spec)"
        " SELECT mission_id, 2, content_hash, spec FROM mission_versions"
        " WHERE mission_id = :mission AND version = 1",
        mission=answer,
    )

    for mission, version in ((file, 2), (file, 99), (12345678, 1)):
        with pytest.raises(IntegrityError, match="fk_user_missions_mission_id_mission_versions"):
            complete(client, ana, mission, version)
    with pytest.raises(IntegrityError, match="fk_user_missions_user_id_users"):
        complete(client, uuid.uuid4(), file)
    complete(client, ana, answer, 2)
    assert progress(client) == [(ana, answer, 2)]


@pytest.mark.parametrize(
    "change",
    [
        "completed_version = completed_version",
        "completed_at = completed_at - interval '1 day'",
        "user_id = gen_random_uuid()",
    ],
)
def test_a_completion_cannot_be_changed(
    client: TestClient, missions: tuple[int, int], change: str
) -> None:
    ana = add_user(client)
    complete(client, ana, missions[0])

    with pytest.raises(DBAPIError, match="user missions cannot be changed"):
        sql(client, f"UPDATE user_missions SET {change}")
    assert progress(client) == [(ana, missions[0], 1)]


def test_deleting_a_user_deletes_their_progress_only(
    client: TestClient, missions: tuple[int, int]
) -> None:
    ana, bia = add_user(client), add_user(client, "bia@example.com")
    complete(client, ana, missions[0])
    complete(client, bia, missions[0])

    sql(client, "DELETE FROM users WHERE id = :id", id=ana)

    assert progress(client) == [(bia, missions[0], 1)]


def test_a_completed_version_still_cannot_be_deleted(
    client: TestClient, missions: tuple[int, int]
) -> None:
    complete(client, add_user(client), missions[0])
    with pytest.raises(DBAPIError, match="mission versions cannot be changed or deleted"):
        sql(client, "DELETE FROM mission_versions WHERE mission_id = :id", id=missions[0])
