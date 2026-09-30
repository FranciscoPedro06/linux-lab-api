"""Sign-up, login, sessions and logout against PostgreSQL."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi.testclient import TestClient

from linuxlab.auth.passwords import Passwords
from linuxlab.auth.sessions import SESSION_IDLE, TOUCH_INTERVAL

from .conftest import AppFactory
from .support import (
    INVITE_CODE,
    ORIGIN,
    PASSWORD,
    login,
    logout,
    me,
    request,
    run,
    session_token,
    sha256,
    signup,
    sql,
)

pytestmark = pytest.mark.integration

WRONG_PASSWORD = "synthetic password 02"


def error_code(response: httpx2.Response) -> str:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    code: str = body["error"]["code"]
    return code


class CountingPasswords(Passwords):
    """Records every Argon2 operation the API asks for."""

    def __init__(self) -> None:
        super().__init__()
        self.hashed = 0
        self.verified: list[str] = []

    async def hash(self, password: str) -> str:
        self.hashed += 1
        return await super().hash(password)

    async def verify(self, password_hash: str, password: str) -> bool:
        self.verified.append(password_hash)
        return await super().verify(password_hash, password)


def count_argon2(client: TestClient) -> CountingPasswords:
    passwords = CountingPasswords()
    client.app.state.passwords = passwords  # type: ignore[attr-defined]
    return passwords


# Sign-up


def test_signup_creates_account_and_session(client: TestClient) -> None:
    response = signup(client, "  Ana.Silva@Example.com ", display_name="  Ana Silva ")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "display_name"}
    assert body["email"] == "ana.silva@example.com"
    assert body["display_name"] == "Ana Silva"
    assert me(client, session_token(response)).json() == body


def test_signup_stores_only_an_argon2id_hash(client: TestClient) -> None:
    signup(client)

    [(password_hash,)] = sql(client, "SELECT password_hash FROM users")
    assert password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash


@pytest.mark.parametrize("invite", ["wrong-invite", "", INVITE_CODE.upper()])
def test_signup_requires_the_invite_code(client: TestClient, invite: str) -> None:
    response = signup(client, invite_code=invite)

    assert response.status_code == 403
    assert error_code(response) == "invalid_invite_code"
    assert sql(client, "SELECT count(*) FROM users") == [(0,)]


def test_signup_without_invite_field_is_malformed(client: TestClient) -> None:
    body = {"email": "ana@example.com", "password": PASSWORD, "display_name": "Ana"}

    response = request(client, "POST", "/api/auth/signup", json=body)

    assert response.status_code == 422
    assert error_code(response) == "invalid_request"


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_signup_disabled_without_configured_invite(
    make_client: AppFactory, configured: str | None
) -> None:
    client = make_client(signup_invite_code=configured)

    response = signup(client, invite_code="")

    assert response.status_code == 403
    assert error_code(response) == "signup_disabled"
    assert sql(client, "SELECT count(*) FROM users") == [(0,)]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("email", "not-an-email", "invalid_email"),
        ("email", "ána@example.com", "invalid_email"),
        ("password", "a" * 11, "invalid_password"),
        ("password", "a" * 129, "invalid_password"),
        ("display_name", "   ", "invalid_display_name"),
        ("display_name", "a" * 81, "invalid_display_name"),
        ("display_name", "Ana\u0000", "invalid_display_name"),
    ],
)
def test_signup_rejects_invalid_fields(
    client: TestClient, field: str, value: str, code: str
) -> None:
    passwords = count_argon2(client)

    response = signup(client, **{field: value})

    assert response.status_code == 422
    assert error_code(response) == code
    assert sql(client, "SELECT count(*) FROM users") == [(0,)]
    assert passwords.hashed == 0


def test_signup_password_bounds_are_inclusive(client: TestClient) -> None:
    assert signup(client, "a@example.com", password="a" * 12).status_code == 201
    assert signup(client, "b@example.com", password="a" * 128).status_code == 201


def test_signup_keeps_display_name_markup_as_text(client: TestClient) -> None:
    response = signup(client, display_name="<script>x</script>")

    assert response.json()["display_name"] == "<script>x</script>"


def test_duplicate_email_ignores_case(client: TestClient) -> None:
    assert signup(client, "ana@example.com").status_code == 201

    response = signup(client, "  ANA@Example.com")

    assert response.status_code == 409
    assert error_code(response) == "email_taken"
    assert sql(client, "SELECT count(*) FROM users") == [(1,)]


def test_concurrent_signups_with_same_email(client: TestClient) -> None:
    body = {
        "email": "ana@example.com",
        "password": PASSWORD,
        "display_name": "Ana",
        "invite_code": INVITE_CODE,
    }

    async def both() -> list[httpx2.Response]:
        transport = httpx2.ASGITransport(app=client.app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="https://testserver", headers={"origin": ORIGIN}
        ) as http:
            return list(
                await asyncio.gather(
                    http.post("/api/auth/signup", json=body),
                    http.post("/api/auth/signup", json={**body, "email": "ANA@example.com"}),
                )
            )

    responses = run(client, both)

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert error_code(conflict) == "email_taken"
    assert sql(client, "SELECT count(*) FROM users") == [(1,)]
    assert sql(client, "SELECT count(*) FROM auth_sessions") == [(1,)]


# Login


def test_login_creates_a_new_session(client: TestClient) -> None:
    signup(client)

    response = login(client, "  ANA@example.com ")

    assert response.status_code == 200
    assert response.json()["email"] == "ana@example.com"
    assert me(client, session_token(response)).status_code == 200
    assert sql(client, "SELECT count(*) FROM auth_sessions") == [(2,)]


def test_wrong_password_and_unknown_email_look_the_same(client: TestClient) -> None:
    signup(client)

    wrong_password = login(client, password=WRONG_PASSWORD)
    unknown_email = login(client, "nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert error_code(wrong_password) == "invalid_credentials"
    assert "set-cookie" not in wrong_password.headers
    assert "set-cookie" not in unknown_email.headers


def test_unknown_email_still_runs_argon2(client: TestClient) -> None:
    signup(client)
    passwords = count_argon2(client)

    login(client, "nobody@example.com")
    login(client, password=WRONG_PASSWORD)

    assert len(passwords.verified) == 2
    assert all(verified.startswith("$argon2id$") for verified in passwords.verified)


# Session cookie and stored data


def test_session_cookie_attributes(client: TestClient) -> None:
    response = signup(client)

    [cookie] = response.headers.get_list("set-cookie")
    name_value, *attributes = [part.strip() for part in cookie.split(";")]
    assert name_value.startswith("__Host-sid=")
    lowered = {attribute.lower() for attribute in attributes}
    assert {"httponly", "secure", "samesite=lax", "path=/", "max-age=2592000"} <= lowered
    assert not any(attribute.startswith("domain") for attribute in lowered)


def test_token_is_never_stored(client: TestClient) -> None:
    token = session_token(signup(client))

    assert len(token) >= 43
    [(stored_hash, session_json)] = sql(
        client, "SELECT token_hash, row_to_json(s)::text FROM auth_sessions s"
    )
    assert bytes(stored_hash) == sha256(token)
    assert token not in session_json
    [(users_json,)] = sql(client, "SELECT json_agg(u)::text FROM users u")
    assert token not in users_json


def test_responses_reveal_no_secrets(client: TestClient) -> None:
    created = signup(client)
    token = session_token(created)
    logged_in = login(client)

    for response in (created, logged_in, me(client, token)):
        assert "$argon2" not in response.text
        assert "password" not in response.text
        assert token not in response.text
        assert session_token(logged_in) not in response.text
        assert set(response.json()) == {"id", "email", "display_name"}


# Session lookup


def test_me_without_session(client: TestClient) -> None:
    response = me(client, None)

    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"


@pytest.mark.parametrize("token", ["forged-token", "x" * 500])
def test_me_with_unknown_token_clears_the_cookie(client: TestClient, token: str) -> None:
    response = me(client, token)

    assert response.status_code == 401
    assert response.headers["set-cookie"].startswith("__Host-sid=")
    assert "max-age=0" in response.headers["set-cookie"].lower()


def test_sessions_belong_to_their_user(client: TestClient) -> None:
    ana = session_token(signup(client, "ana@example.com", display_name="Ana"))
    bia = session_token(signup(client, "bia@example.com", display_name="Bia"))

    assert me(client, ana).json()["email"] == "ana@example.com"
    assert me(client, bia).json()["email"] == "bia@example.com"


def age_session(client: TestClient, token: str, *, created: timedelta, seen: timedelta) -> None:
    now = datetime.now(UTC)
    sql(
        client,
        "UPDATE auth_sessions SET created_at = :created, last_seen_at = :seen,"
        " expires_at = :expires WHERE token_hash = :hash",
        created=now - created,
        seen=now - seen,
        expires=now - created + timedelta(days=30),
        hash=sha256(token),
    )


def test_idle_session_expires(client: TestClient) -> None:
    token = session_token(signup(client))
    age_session(client, token, created=timedelta(days=8), seen=SESSION_IDLE + timedelta(minutes=1))

    assert me(client, token).status_code == 401
    assert sql(client, "SELECT count(*) FROM auth_sessions") == [(0,)]


def test_active_session_expires_at_absolute_limit(client: TestClient) -> None:
    token = session_token(signup(client))
    age_session(client, token, created=timedelta(days=30, minutes=1), seen=timedelta(minutes=1))

    assert me(client, token).status_code == 401
    assert sql(client, "SELECT count(*) FROM auth_sessions") == [(0,)]


def test_session_just_inside_both_limits_is_valid(client: TestClient) -> None:
    token = session_token(signup(client))
    age_session(
        client, token, created=timedelta(days=29, hours=23), seen=timedelta(days=6, hours=23)
    )

    assert me(client, token).status_code == 200


def test_activity_is_recorded_at_most_every_touch_interval(client: TestClient) -> None:
    token = session_token(signup(client))

    def times() -> tuple[datetime, datetime]:
        [(seen, expires)] = sql(client, "SELECT last_seen_at, expires_at FROM auth_sessions")
        return seen, expires

    age_session(client, token, created=timedelta(days=2), seen=timedelta(minutes=1))
    before = times()
    me(client, token)
    assert times() == before

    age_session(client, token, created=timedelta(days=2), seen=TOUCH_INTERVAL)
    seen_before, expires_before = times()
    me(client, token)
    seen_after, expires_after = times()
    assert seen_after > seen_before
    assert datetime.now(UTC) - seen_after < timedelta(minutes=1)
    assert expires_after == expires_before


# Logout


def test_logout_ends_only_the_current_session(client: TestClient) -> None:
    first = session_token(signup(client))
    second = session_token(login(client))
    other_user = session_token(signup(client, "bia@example.com"))

    response = logout(client, first)

    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("__host-sid=")
    assert "max-age=0" in cookie
    assert "secure" in cookie
    assert me(client, first).status_code == 401
    assert me(client, second).status_code == 200
    assert me(client, other_user).status_code == 200
    assert sql(client, "SELECT count(*) FROM auth_sessions") == [(2,)]


def test_logout_without_session_is_harmless(client: TestClient) -> None:
    assert logout(client, None).status_code == 204
    assert logout(client, "forged-token").status_code == 204


def test_logout_from_another_origin_is_refused(client: TestClient) -> None:
    token = session_token(signup(client))

    response = request(
        client,
        "POST",
        "/api/auth/logout",
        token=token,
        json={},
        headers={"origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert error_code(response) == "origin_not_allowed"
    assert me(client, token).status_code == 200


def test_login_form_post_is_refused(client: TestClient) -> None:
    signup(client)

    response = request(
        client,
        "POST",
        "/api/auth/login",
        content=f"email=ana@example.com&password={PASSWORD}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415
    assert "set-cookie" not in response.headers


# Rate limiting


def test_login_rate_limit_applies_before_argon2(client: TestClient) -> None:
    signup(client)
    passwords = count_argon2(client)

    attempts = [login(client, password=WRONG_PASSWORD) for _ in range(5)]
    blocked = login(client)

    assert [response.status_code for response in attempts] == [401] * 5
    assert blocked.status_code == 429
    assert error_code(blocked) == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 900
    assert len(passwords.verified) == 5


def test_signup_rate_limit_applies_before_argon2(client: TestClient) -> None:
    passwords = count_argon2(client)

    attempts = [signup(client, f"user{index}@example.com") for index in range(5)]
    blocked = signup(client, "user5@example.com")

    assert [response.status_code for response in attempts] == [201] * 5
    assert blocked.status_code == 429
    assert error_code(blocked) == "rate_limited"
    assert passwords.hashed == 5


def test_login_and_signup_limits_are_separate(client: TestClient) -> None:
    signup(client)
    for _ in range(5):
        login(client, password=WRONG_PASSWORD)

    assert signup(client, "bia@example.com").status_code == 201


# Other routes


def test_health_needs_no_origin(client: TestClient) -> None:
    client.headers.pop("origin")

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_terminal_route_absent_without_dev_access(client: TestClient) -> None:
    assert client.get("/ws/labs/0123/terminal").status_code == 404
