"""The client address behind the reverse proxy, as the production server sees it.

In production uvicorn runs with --proxy-headers and --forwarded-allow-ips set to the
reverse proxy's fixed address only (infra/compose.prod.yml). These tests put the same
uvicorn middleware, configured the same way, in front of the application, and set the
TCP peer of each request, to check which key the authentication rate limits use.
"""

from collections.abc import Iterator
from typing import Any, cast

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.types import Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.main import create_app
from tests.conftest import app_settings

from .support import ORIGIN, PASSWORD, sql

pytestmark = pytest.mark.integration

PROXY = "172.30.1.2"
PEER_HEADER = "x-test-peer"
WRONG_PASSWORD = "synthetic password 02"


class Peer:
    """Sets the request's TCP peer from a test-only header, as the network would."""

    def __init__(self, app: Any, state: Any) -> None:
        self.app = app
        # Read by tests.auth.support.sql through TestClient.app.
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope["headers"])
            peer = headers.get(PEER_HEADER.encode())
            if peer is not None:
                scope["client"] = (peer.decode(), 40000)
            scope["headers"] = [
                (name, value) for name, value in scope["headers"] if name != PEER_HEADER.encode()
            ]
        await self.app(scope, receive, send)


@pytest.fixture
def proxied() -> Iterator[TestClient]:
    app = create_app(app_settings(), lab_runtime=FakeRuntime(), start_reaper=False)
    # uvicorn types applications with its own ASGI protocols; it wraps them the same way.
    server = Peer(ProxyHeadersMiddleware(cast(Any, app), trusted_hosts=PROXY), app.state)
    with TestClient(server, base_url="https://testserver", headers={"origin": ORIGIN}) as client:
        sql(
            client,
            "TRUNCATE users, auth_sessions, lab_sessions, validation_runs, user_missions,"
            " command_history, modules, missions, mission_versions",
        )
        yield client


def login(client: TestClient, peer: str, forwarded_for: str | None = None) -> httpx2.Response:
    headers = {PEER_HEADER: peer}
    if forwarded_for is not None:
        headers["x-forwarded-for"] = forwarded_for
    body = {"email": "ana@example.com", "password": WRONG_PASSWORD}
    return client.post("/api/auth/login", json=body, headers=headers)


def exhaust(client: TestClient, peer: str, forwarded_for: str | None = None) -> None:
    statuses = [login(client, peer, forwarded_for).status_code for _ in range(5)]
    assert statuses == [401] * 5


def test_clients_behind_the_proxy_have_separate_limits(proxied: TestClient) -> None:
    exhaust(proxied, PROXY, "198.51.100.1")

    assert login(proxied, PROXY, "198.51.100.1").status_code == 429
    assert login(proxied, PROXY, "198.51.100.2").status_code == 401


def test_a_value_the_client_put_before_the_proxys_does_not_change_the_key(
    proxied: TestClient,
) -> None:
    # The proxy appends the address it saw; whatever the client sent comes first.
    exhaust(proxied, PROXY, "198.51.100.1")

    assert login(proxied, PROXY, "203.0.113.9, 198.51.100.1").status_code == 429
    assert login(proxied, PROXY, "198.51.100.2, 198.51.100.1").status_code == 429


def test_forwarded_for_from_an_untrusted_peer_is_ignored(proxied: TestClient) -> None:
    for index in range(5):
        assert login(proxied, "203.0.113.5", f"198.51.100.{index + 10}").status_code == 401

    assert login(proxied, "203.0.113.5", "198.51.100.99").status_code == 429
    # The addresses it named were never charged.
    assert login(proxied, PROXY, "198.51.100.10").status_code == 401


def test_an_untrusted_peer_cannot_spend_another_clients_budget(proxied: TestClient) -> None:
    exhaust(proxied, "203.0.113.5", "198.51.100.1")

    assert login(proxied, PROXY, "198.51.100.1").status_code == 401


def test_signup_limit_uses_the_same_key(proxied: TestClient) -> None:
    def signup(forwarded_for: str, index: int) -> int:
        body = {
            "email": f"user{index}@example.com",
            "password": PASSWORD,
            "display_name": "Ana",
            "invite_code": "wrong",
        }
        headers = {PEER_HEADER: PROXY, "x-forwarded-for": forwarded_for}
        return proxied.post("/api/auth/signup", json=body, headers=headers).status_code

    assert [signup("198.51.100.1", index) for index in range(5)] == [403] * 5
    assert signup("198.51.100.1", 5) == 429
    assert signup("198.51.100.2", 6) == 403
