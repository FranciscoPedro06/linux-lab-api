"""Constraints enforced by the database itself, independent of the API."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from .support import sql

pytestmark = pytest.mark.integration

HASH = "$argon2id$synthetic"


def add_user(client: TestClient, email: str, display_name: str = "Ana") -> uuid.UUID:
    user_id = uuid.uuid4()
    sql(
        client,
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (:id, :email, :hash, :name)",
        id=user_id,
        email=email,
        hash=HASH,
        name=display_name,
    )
    return user_id


def add_session(client: TestClient, user_id: uuid.UUID, token_hash: bytes) -> None:
    sql(
        client,
        "INSERT INTO auth_sessions (user_id, token_hash, created_at, last_seen_at, expires_at)"
        " VALUES (:user_id, :hash, now(), now(), now() + interval '30 days')",
        user_id=user_id,
        hash=token_hash,
    )


def test_email_is_unique(client: TestClient) -> None:
    add_user(client, "ana@example.com")

    with pytest.raises(IntegrityError, match="uq_users_email_lower"):
        add_user(client, "ana@example.com")


@pytest.mark.parametrize("email", ["Ana@example.com", " ana@example.com"])
def test_email_must_be_stored_normalized(client: TestClient, email: str) -> None:
    with pytest.raises(IntegrityError, match="ck_users_email_normalized"):
        add_user(client, email)


@pytest.mark.parametrize("name", ["", "a" * 81])
def test_display_name_length(client: TestClient, name: str) -> None:
    with pytest.raises(IntegrityError, match="ck_users_display_name_length"):
        add_user(client, "ana@example.com", name)


def test_token_hash_is_unique_and_sha256_sized(client: TestClient) -> None:
    user_id = add_user(client, "ana@example.com")
    add_session(client, user_id, b"\x01" * 32)

    with pytest.raises(IntegrityError, match="uq_auth_sessions_token_hash"):
        add_session(client, user_id, b"\x01" * 32)
    with pytest.raises(IntegrityError, match="ck_auth_sessions_token_hash_length"):
        add_session(client, user_id, b"\x02" * 16)


def test_session_needs_an_existing_user(client: TestClient) -> None:
    with pytest.raises(IntegrityError, match="fk_auth_sessions_user_id_users"):
        add_session(client, uuid.uuid4(), b"\x01" * 32)


def test_deleting_a_user_deletes_their_sessions(client: TestClient) -> None:
    user_id = add_user(client, "ana@example.com")
    add_session(client, user_id, b"\x01" * 32)

    sql(client, "DELETE FROM users WHERE id = :id", id=user_id)

    assert sql(client, "SELECT count(*) FROM auth_sessions") == [(0,)]
