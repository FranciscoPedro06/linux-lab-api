"""Logout ends the user's active lab, then the session."""

import pytest
from fastapi.testclient import TestClient

from linuxlab.labs.lifecycle import Labs
from tests.auth.support import login, logout, me, session_token

from .lab_support import (
    container,
    create_lab,
    current_lab,
    fake_runtime,
    hold,
    in_background,
    lab_row,
    labs,
    ready_lab,
    user,
    wait_for_call,
)

pytestmark = pytest.mark.integration


def test_logout_ends_the_active_lab_and_the_session(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    assert logout(client, ana).status_code == 204

    assert lab_row(client, lab["id"]) == ("terminated", "logout")
    assert container(client, lab["id"]) is None
    assert me(client, ana).status_code == 401


def test_logout_of_one_user_leaves_other_users_labs_alone(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab_a = ready_lab(client, ana)
    lab_b = ready_lab(client, bia)

    logout(client, ana)

    assert lab_row(client, lab_a["id"]) == ("terminated", "logout")
    assert lab_row(client, lab_b["id"]) == ("ready", None)
    info = container(client, lab_b["id"])
    assert info is not None and info.running
    assert me(client, bia).status_code == 200


def test_logout_ends_the_lab_for_every_session_of_the_user(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    other_device = session_token(login(client, "ana@example.com"))
    lab = ready_lab(client, ana)

    logout(client, ana)

    # The other session stays valid, but the lab belonged to the user, not the session.
    assert me(client, other_device).status_code == 200
    assert current_lab(client, other_device) is None
    assert lab_row(client, lab["id"]) == ("terminated", "logout")


def test_logout_without_a_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")

    assert logout(client, ana).status_code == 204
    assert me(client, ana).status_code == 401


@pytest.mark.parametrize("token", [None, "forged-token"])
def test_logout_without_a_valid_session_touches_no_lab(
    client: TestClient, token: str | None
) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    assert logout(client, token).status_code == 204

    assert lab_row(client, lab["id"]) == ("ready", None)


def test_logout_while_the_lab_is_provisioning(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    release = hold(client, "start")
    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "start")

    assert logout(client, ana).status_code == 204
    release()

    created = creating.result(10)
    assert created.json()["status"] == "terminated"
    assert created.json()["end_reason"] == "logout"
    assert fake_runtime(client).containers() == []


def test_logout_completes_when_only_the_container_removal_fails(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")

    assert logout(client, ana).status_code == 204

    # Ended as far as the user is concerned; the reaper removes the container later.
    assert lab_row(client, lab["id"]) == ("terminating", "logout")
    assert me(client, ana).status_code == 401


def test_session_is_kept_when_the_lab_cannot_be_ended(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)

    async def broken(self: Labs, *args: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(type(labs(client)), "end_active", broken)
    # The client gets 500 internal_error; TestClient re-raises the server error instead.
    with pytest.raises(RuntimeError, match="database went away"):
        logout(client, ana)

    assert me(client, ana).status_code == 200
    assert lab_row(client, lab["id"]) == ("ready", None)
