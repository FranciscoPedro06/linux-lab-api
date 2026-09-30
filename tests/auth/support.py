"""Runs the API against the PostgreSQL at DATABASE_URL, with synthetic accounts."""

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from linuxlab.auth.cookies import SESSION_COOKIE

ORIGIN = "http://localhost:5173"
INVITE_CODE = "synthetic-invite-for-tests"
PASSWORD = "synthetic password 01"


def run[T](client: TestClient, function: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine on the application's event loop."""
    assert client.portal is not None
    return client.portal.call(function)


def sql(client: TestClient, statement: str, **params: Any) -> list[Any]:
    engine: AsyncEngine = client.app.state.engine  # type: ignore[attr-defined]

    async def execute() -> list[Any]:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []

    return run(client, execute)


def request(
    client: TestClient, method: str, path: str, *, token: str | None = None, **kwargs: Any
) -> Response:
    """Send a request carrying exactly the given session token, or none."""
    client.cookies.clear()
    headers = dict(kwargs.pop("headers", {}))
    if token is not None:
        headers["cookie"] = f"{SESSION_COOKIE}={token}"
    response = client.request(method, path, headers=headers, **kwargs)
    client.cookies.clear()
    return response


def signup(
    client: TestClient,
    email: str = "ana@example.com",
    *,
    password: str = PASSWORD,
    display_name: str = "Ana",
    invite_code: str = INVITE_CODE,
) -> Response:
    body = {
        "email": email,
        "password": password,
        "display_name": display_name,
        "invite_code": invite_code,
    }
    return request(client, "POST", "/api/auth/signup", json=body)


def login(client: TestClient, email: str = "ana@example.com", password: str = PASSWORD) -> Response:
    return request(client, "POST", "/api/auth/login", json={"email": email, "password": password})


def me(client: TestClient, token: str | None) -> Response:
    return request(client, "GET", "/api/auth/me", token=token)


def logout(client: TestClient, token: str | None) -> Response:
    return request(client, "POST", "/api/auth/logout", token=token, json={})


def session_token(response: Response) -> str:
    token = response.cookies.get(SESSION_COOKIE)
    assert token, response.headers.get("set-cookie")
    return token


def sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
