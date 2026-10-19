"""Logout racing a lab creation or replacement.

Once a logout has been answered, no lab requested with its session is left active:
a lab inserted before the session was revoked is ended by the logout, and one asked
for after it is refused. PostgreSQL with labs on a FakeRuntime; the order is forced
with runtime gates and events, never with sleeps.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from linuxlab.auth import sessions
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.models import ACTIVE_STATUSES_SQL
from tests.auth.support import login, logout, me, session_token, sha256, sql

from .lab_support import (
    create_lab,
    fake_runtime,
    hold,
    in_background,
    lab_row,
    labs,
    ready_lab,
    user,
    wait_for_call,
)
from .test_lab_replace import (
    assert_invariants,
    patch_before_successor,
    replace_lab,
    successors,
    wait_for_calls,
)

pytestmark = pytest.mark.integration


def session_exists(client: TestClient, token: str) -> bool:
    rows = sql(client, "SELECT 1 FROM auth_sessions WHERE token_hash = :h", h=sha256(token))
    return rows != []


def active_labs(client: TestClient) -> list[tuple[str, str]]:
    rows = sql(
        client, f"SELECT id::text, status FROM lab_sessions WHERE status IN ({ACTIVE_STATUSES_SQL})"
    )
    return [tuple(row) for row in rows]


def assert_logged_out(client: TestClient, token: str) -> None:
    """The session is gone, and so is every lab and container."""
    assert not session_exists(client, token)
    assert me(client, token).status_code == 401
    assert active_labs(client) == []
    assert fake_runtime(client).containers() == []
    assert_invariants(client)


async def logout_meanwhile(client: TestClient, token: str) -> None:
    """A whole logout, from another request, while the caller waits for it."""
    response = await asyncio.to_thread(logout, client, token)
    assert response.status_code == 204


def patch_after_first_pass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, Callable[[], None]]:
    """Logout's first pass over the user's labs sets the returned event; its session is
    revoked only once the returned function is called."""
    service = labs(client)
    first_pass = asyncio.Event()
    revoke_now = asyncio.Event()
    original_end_active = service.end_active
    original_revoke = sessions.revoke

    async def end_active(*args: Any) -> Any:
        try:
            return await original_end_active(*args)
        finally:
            first_pass.set()

    async def revoke(*args: Any) -> None:
        await revoke_now.wait()
        await original_revoke(*args)

    monkeypatch.setattr(service, "end_active", end_active)
    monkeypatch.setattr(sessions, "revoke", revoke)

    def allow_revoke() -> None:
        assert client.portal is not None
        client.portal.call(revoke_now.set)

    return first_pass, allow_revoke


# Replacement


def test_logout_during_the_removal_of_the_replaced_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "remove")
    replacing = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "remove", 1)
    logging_out = in_background(lambda: logout(client, ana))
    # Logout's own pass over the lab being removed.
    wait_for_calls(client, "remove", 2)
    release()

    assert logging_out.result(timeout=10).status_code == 204
    response = replacing.result(timeout=10)
    # Whichever came first, the session's replacement leaves no lab behind.
    if response.status_code == 201:
        assert response.json()["end_reason"] == "logout"
    else:
        assert response.status_code == 401, response.text
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert [s[1:] for s in successors(client, first["id"])] in ([], [("terminated", "logout")])
    assert_logged_out(client, ana)


def test_logout_confirmed_before_the_successor_is_inserted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    async def the_user_logs_out(service: Labs) -> None:
        await logout_meanwhile(client, ana)

    patch_before_successor(client, monkeypatch, the_user_logs_out)
    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "not_authenticated"
    assert lab_row(client, first["id"]) == ("terminated", "reset")
    assert successors(client, first["id"]) == []
    assert_logged_out(client, ana)


def test_replacement_asked_after_the_logout_is_refused(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    assert logout(client, ana).status_code == 204
    calls = len(fake_runtime(client).calls)

    response = replace_lab(client, ana, first["id"])

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"
    assert lab_row(client, first["id"]) == ("terminated", "logout")
    assert successors(client, first["id"]) == []
    assert len(fake_runtime(client).calls) == calls
    assert_logged_out(client, ana)


def test_successor_inserted_before_the_session_is_revoked_is_ended_by_the_logout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    first_pass, allow_revoke = patch_after_first_pass(client, monkeypatch)
    logging_out: list[Any] = []

    async def logout_passes_over_the_labs_first(service: Labs) -> None:
        # The replaced lab is gone: logout's first pass finds no lab. The successor is
        # inserted after that pass and before the revocation.
        logging_out.append(in_background(lambda: logout(client, ana)))
        await first_pass.wait()

    patch_before_successor(client, monkeypatch, logout_passes_over_the_labs_first)
    release = hold(client, "start")
    replacing = in_background(lambda: replace_lab(client, ana, first["id"]))
    wait_for_calls(client, "start", 2)
    allow_revoke()

    assert logging_out[0].result(timeout=10).status_code == 204
    # Answered: the successor has already been ended, before its container started.
    [(_, status, reason)] = successors(client, first["id"])
    assert (status, reason) == ("terminated", "logout")
    release()

    response = replacing.result(timeout=10)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "terminated"
    assert response.json()["end_reason"] == "logout"
    assert_logged_out(client, ana)


def test_logout_first_then_the_replacement_of_the_lab_it_is_removing(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    release = hold(client, "remove")
    logging_out = in_background(lambda: logout(client, ana))
    wait_for_calls(client, "remove", 1)

    # The session is still valid, but its lab is already ending for the logout.
    response = replace_lab(client, ana, first["id"])
    release()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_current"
    assert logging_out.result(timeout=10).status_code == 204
    assert lab_row(client, first["id"]) == ("terminated", "logout")
    assert successors(client, first["id"]) == []
    assert_logged_out(client, ana)


def test_asking_again_after_a_lost_answer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    async def the_user_logs_out(service: Labs) -> None:
        await logout_meanwhile(client, ana)

    patch_before_successor(client, monkeypatch, the_user_logs_out)
    replace_lab(client, ana, first["id"])  # The answer is lost.
    monkeypatch.undo()

    # With the old session: refused, nothing created.
    response = replace_lab(client, ana, first["id"])
    assert response.status_code == 401
    assert successors(client, first["id"]) == []
    assert_logged_out(client, ana)

    # Signed in again, the user asks for it again and the replacement is resumed once.
    again = session_token(login(client, "ana@example.com"))
    response = replace_lab(client, again, first["id"])
    assert response.status_code == 201, response.text
    retried = replace_lab(client, again, first["id"])
    assert retried.status_code == 200
    assert retried.json()["id"] == response.json()["id"]
    assert [s[1:] for s in successors(client, first["id"])] == [("ready", None)]
    assert_invariants(client)


# Creation


def test_logout_confirmed_before_a_new_lab_is_inserted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    service = labs(client)
    original = service._target

    async def the_user_logs_out(*args: Any) -> Any:
        target = await original(*args)
        await logout_meanwhile(client, ana)
        return target

    monkeypatch.setattr(service, "_target", the_user_logs_out)
    response = create_lab(client, ana)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "not_authenticated"
    assert sql(client, "SELECT count(*) FROM lab_sessions")[0][0] == 0
    assert_logged_out(client, ana)


def test_lab_inserted_before_the_session_is_revoked_is_ended_by_the_logout(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    first_pass, allow_revoke = patch_after_first_pass(client, monkeypatch)
    service = labs(client)
    original = service._target
    logging_out: list[Any] = []

    async def logout_passes_over_the_labs_first(*args: Any) -> Any:
        # Logout's first pass finds no lab; the lab is inserted after that pass and
        # before the revocation.
        logging_out.append(in_background(lambda: logout(client, ana)))
        await first_pass.wait()
        return await original(*args)

    monkeypatch.setattr(service, "_target", logout_passes_over_the_labs_first)
    release = hold(client, "start")
    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "start")
    allow_revoke()

    assert logging_out[0].result(timeout=10).status_code == 204
    [(_, status, reason)] = sql(client, "SELECT id, status, end_reason FROM lab_sessions")
    assert (status, reason) == ("terminated", "logout")
    release()

    created = creating.result(timeout=10)
    assert created.status_code == 201, created.text
    assert created.json()["end_reason"] == "logout"
    assert_logged_out(client, ana)


def test_another_sessions_lab_after_the_logout_is_kept(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    other_device = session_token(login(client, "ana@example.com"))
    assert logout(client, ana).status_code == 204

    # Logout ends the user's lab, not the user's other sessions.
    lab = ready_lab(client, other_device)

    assert lab_row(client, lab["id"]) == ("ready", None)
    assert create_lab(client, ana).status_code == 401
