"""Reconciliation of lab sessions with containers, against PostgreSQL and a FakeRuntime."""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from linuxlab.labs.lifecycle import (
    MAX_LIFETIME,
    NO_INPUT_TIMEOUT,
    NO_TERMINAL_TIMEOUT,
    PROVISIONING_TIMEOUT,
    TERMINATION_GRACE,
)
from linuxlab.labs.runtime import ContainerInfo
from linuxlab.labs.runtime.spec import container_name
from linuxlab.labs.terminal.relay import Outcome, TerminalRegistry
from linuxlab.main import create_app
from tests.auth.support import ORIGIN, sql
from tests.conftest import AppFactory, app_settings

from .lab_support import (
    container,
    create_lab,
    end_lab,
    fake_runtime,
    hold,
    in_background,
    lab_row,
    labs,
    ready_lab,
    run_async,
    user,
    wait_for_call,
)

pytestmark = pytest.mark.integration


def reconcile(client: TestClient, *, after: timedelta = timedelta(), startup: bool = False) -> None:
    now = datetime.now(UTC) + after

    async def run() -> None:
        await labs(client).reconcile(startup=startup, now=now)

    run_async(client, run)


def idle_for(client: TestClient, lab_id: str, idle: timedelta) -> None:
    sql(
        client,
        "UPDATE lab_sessions SET last_activity_at = now() - CAST(:idle AS interval) WHERE id = :id",
        idle=idle,
        id=lab_id,
    )


def registry(client: TestClient) -> TerminalRegistry:
    terminals: TerminalRegistry = client.app.state.terminals  # type: ignore[attr-defined]
    return terminals


class FakeConnection:
    """Holds a lab's terminal claim the way a WebSocket connection does."""

    def __init__(self, client: TestClient, lab_id: str) -> None:
        self.outcome: Outcome | None = None
        self.released_at: float | None = None
        self._client = client
        self._key = uuid.UUID(lab_id).hex
        self._task: Any = None
        self._close: asyncio.Event | None = None

    def open(self) -> "FakeConnection":
        async def start() -> None:
            self._close = asyncio.Event()
            claimed = asyncio.Event()
            self._task = asyncio.create_task(self._hold(claimed))
            await claimed.wait()

        run_async(self._client, start)
        return self

    def close(self) -> None:
        async def stop() -> None:
            assert self._close is not None
            self._close.set()
            await self._task

        run_async(self._client, stop)

    async def _hold(self, claimed: asyncio.Event) -> None:
        assert self._close is not None
        async with registry(self._client).claim(self._key) as claim:
            claimed.set()
            stop = asyncio.create_task(claim.stop.wait())
            close = asyncio.create_task(self._close.wait())
            await asyncio.wait([stop, close], return_when=asyncio.FIRST_COMPLETED)
            stop.cancel()
            close.cancel()
            if claim.stop.is_set():
                self.outcome = claim.outcome
                await asyncio.sleep(0.05)  # the terminal's cleanup
        self.released_at = time.monotonic()


# Timeouts


def test_lab_past_its_maximum_lifetime_is_ended(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    # Recent activity as seen from the shifted clock, so only the lifetime can expire.
    idle_for(client, lab["id"], -MAX_LIFETIME)

    reconcile(client, after=MAX_LIFETIME - timedelta(seconds=5))
    assert lab_row(client, lab["id"]) == ("ready", None)

    reconcile(client, after=MAX_LIFETIME)
    assert lab_row(client, lab["id"]) == ("terminated", "max_lifetime")
    assert container(client, lab["id"]) is None


def test_lab_without_a_terminal_is_ended_after_15_minutes(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))

    idle_for(client, lab["id"], NO_TERMINAL_TIMEOUT - timedelta(minutes=1))
    reconcile(client)
    assert lab_row(client, lab["id"]) == ("ready", None)

    idle_for(client, lab["id"], NO_TERMINAL_TIMEOUT)
    reconcile(client)
    assert lab_row(client, lab["id"]) == ("terminated", "no_terminal")
    assert container(client, lab["id"]) is None


def test_connected_terminal_without_input_ends_the_lab_after_30_minutes(
    client: TestClient,
) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    connection = FakeConnection(client, lab["id"]).open()

    # Connected: the 15 minute rule does not apply.
    idle_for(client, lab["id"], NO_TERMINAL_TIMEOUT + timedelta(minutes=5))
    run_async(client, _forget_activity(client))
    reconcile(client)
    assert lab_row(client, lab["id"]) == ("ready", None)

    idle_for(client, lab["id"], NO_INPUT_TIMEOUT)
    reconcile(client)

    assert lab_row(client, lab["id"]) == ("terminated", "no_input")
    assert connection.outcome is Outcome.LAB_ENDED
    assert container(client, lab["id"]) is None


def test_terminal_activity_is_stored_and_postpones_the_timeout(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    idle_for(client, lab["id"], NO_TERMINAL_TIMEOUT)

    registry(client).touch(uuid.UUID(lab["id"]).hex)
    reconcile(client)

    assert lab_row(client, lab["id"]) == ("ready", None)
    seconds = sql(
        client,
        "SELECT extract(epoch FROM now() - last_activity_at) FROM lab_sessions WHERE id = :id",
        id=lab["id"],
    )
    assert float(seconds[0][0]) < 60


def test_lab_stuck_in_provisioning_fails(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    release = hold(client, "start")
    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "start")
    lab_id = sql(client, "SELECT id FROM lab_sessions")[0][0]

    reconcile(client, after=PROVISIONING_TIMEOUT - timedelta(seconds=5))
    assert lab_row(client, str(lab_id)) == ("provisioning", None)

    reconcile(client, after=PROVISIONING_TIMEOUT + timedelta(seconds=1))
    assert lab_row(client, str(lab_id)) == ("terminated", "provisioning_timeout")
    release()

    response = creating.result(10)
    assert response.status_code == 201
    assert response.json()["status"] == "terminated"
    assert fake_runtime(client).containers() == []


# Containers that stopped or disappeared


@pytest.mark.parametrize(("oom", "reason"), [(True, "oom"), (False, "container_lost")])
def test_stopped_container_ends_the_lab(client: TestClient, oom: bool, reason: str) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    info = container(client, lab["id"])
    assert info is not None
    fake_runtime(client).crash(info.id, oom=oom)

    reconcile(client)

    assert lab_row(client, lab["id"]) == ("terminated", reason)
    assert container(client, lab["id"]) is None


def test_missing_container_ends_the_lab(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    info = container(client, lab["id"])
    assert info is not None
    run_async(client, lambda: fake_runtime(client).remove(info.id))

    reconcile(client)

    assert lab_row(client, lab["id"]) == ("terminated", "container_lost")


def test_container_missing_from_a_stale_listing_is_inspected_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))

    async def empty_listing(deployment: str) -> list[ContainerInfo]:
        return []

    monkeypatch.setattr(fake_runtime(client), "list_labs", empty_listing)
    reconcile(client)

    assert lab_row(client, lab["id"]) == ("ready", None)


def test_container_with_foreign_labels_is_not_the_labs(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    info = container(client, lab["id"])
    assert info is not None
    run_async(client, lambda: fake_runtime(client).remove(info.id))
    name = container_name(uuid.UUID(lab["id"]).hex)
    impostor = fake_runtime(client).add_container(name, {"linuxlab.managed": "true"})

    reconcile(client)

    assert lab_row(client, lab["id"]) == ("terminated", "container_lost")
    assert impostor in [c.id for c in fake_runtime(client).containers()]


# Unfinished endings


def test_terminating_lab_is_finished_after_the_grace_period(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")
    assert end_lab(client, ana, lab["id"]).json()["status"] == "terminating"
    fake_runtime(client).fail.clear()

    reconcile(client)
    assert lab_row(client, lab["id"]) == ("terminating", "user")

    reconcile(client, after=TERMINATION_GRACE + timedelta(seconds=1))
    assert lab_row(client, lab["id"]) == ("terminated", "user")
    assert container(client, lab["id"]) is None


def test_startup_finishes_terminating_labs_at_once(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")
    end_lab(client, ana, lab["id"])
    fake_runtime(client).fail.clear()

    reconcile(client, startup=True)

    assert lab_row(client, lab["id"]) == ("terminated", "user")


def test_failed_lab_is_finished_once_docker_is_back(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    fake_runtime(client).available = False
    assert create_lab(client, ana).status_code == 503
    fake_runtime(client).available = True

    reconcile(client, startup=True)

    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("terminated", "provisioning_failed")
    ]


def test_unreachable_docker_changes_nothing(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    fake_runtime(client).available = False

    with pytest.raises(Exception, match=r"unavailable|failed"):
        reconcile(client, startup=True)

    assert lab_row(client, lab["id"]) == ("ready", None)


# Orphans


def test_orphan_containers_of_this_deployment_are_removed(make_client: AppFactory) -> None:
    client = make_client(lab_deployment="reaper-tests")
    runtime = fake_runtime(client)
    lab = ready_lab(client, user(client, "ana@example.com"))
    orphan_key = uuid.uuid4().hex
    orphan = runtime.add_container(
        container_name(orphan_key),
        {
            "linuxlab.managed": "true",
            "linuxlab.lab_id": orphan_key,
            "linuxlab.deployment": "reaper-tests",
        },
    )
    other_key = uuid.uuid4().hex
    other_deployment = runtime.add_container(
        container_name(other_key),
        {"linuxlab.managed": "true", "linuxlab.lab_id": other_key, "linuxlab.deployment": "prod"},
    )
    unmanaged = runtime.add_container(
        container_name(uuid.uuid4().hex), {"linuxlab.deployment": "reaper-tests"}
    )
    misnamed = runtime.add_container(
        "something-else",
        {
            "linuxlab.managed": "true",
            "linuxlab.lab_id": uuid.uuid4().hex,
            "linuxlab.deployment": "reaper-tests",
        },
    )

    reconcile(client)

    remaining = {c.id for c in runtime.containers()}
    assert orphan not in remaining
    assert {other_deployment, unmanaged, misnamed} <= remaining
    assert lab_row(client, lab["id"]) == ("ready", None)
    assert container(client, lab["id"]) is not None


def test_container_of_a_terminated_lab_is_removed(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    key = uuid.UUID(lab["id"]).hex
    end_lab(client, ana, lab["id"])
    # A container recreated for an ended lab, e.g. by a request that crashed midway.
    fake_runtime(client).add_container(
        container_name(key),
        {"linuxlab.managed": "true", "linuxlab.lab_id": key, "linuxlab.deployment": "default"},
    )

    reconcile(client)

    assert container(client, lab["id"]) is None


# Isolation between users


def test_reaping_one_users_lab_leaves_others_alone(client: TestClient) -> None:
    lab_a = ready_lab(client, user(client, "ana@example.com"))
    lab_b = ready_lab(client, user(client, "bia@example.com"))
    connection_b = FakeConnection(client, lab_b["id"]).open()
    idle_for(client, lab_a["id"], NO_TERMINAL_TIMEOUT)

    reconcile(client)

    assert lab_row(client, lab_a["id"]) == ("terminated", "no_terminal")
    assert lab_row(client, lab_b["id"]) == ("ready", None)
    info = container(client, lab_b["id"])
    assert info is not None and info.running
    assert connection_b.outcome is None
    connection_b.close()


def test_terminal_is_closed_before_the_container_is_removed(client: TestClient) -> None:
    lab = ready_lab(client, user(client, "ana@example.com"))
    connection = FakeConnection(client, lab["id"]).open()
    removed_at: list[float] = []
    runtime = fake_runtime(client)
    remove = runtime.remove

    async def recording_remove(container_id: str) -> None:
        removed_at.append(time.monotonic())
        await remove(container_id)

    runtime.remove = recording_remove  # type: ignore[method-assign]
    run_async(client, _forget_activity(client))
    idle_for(client, lab["id"], NO_INPUT_TIMEOUT)
    reconcile(client)

    assert connection.outcome is Outcome.LAB_ENDED
    assert connection.released_at is not None
    assert removed_at and connection.released_at <= removed_at[0]


# Startup


def test_reaper_runs_at_startup_and_keeps_live_labs(make_client: AppFactory) -> None:
    client = make_client()
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    live = ready_lab(client, ana)
    stale = ready_lab(client, bia)
    fake_runtime(client).fail.add("remove")
    end_lab(client, bia, stale["id"])
    runtime = fake_runtime(client)
    runtime.fail.clear()

    # A new API process on the same database and Docker Engine.
    app = create_app(app_settings(), lab_runtime=runtime, start_reaper=True)
    with TestClient(app, base_url="https://testserver", headers={"origin": ORIGIN}) as restarted:
        deadline = time.monotonic() + 5
        while lab_row(restarted, stale["id"])[0] != "terminated":
            assert time.monotonic() < deadline
            time.sleep(0.05)

        assert lab_row(restarted, live["id"]) == ("ready", None)
        info = container(restarted, live["id"])
        assert info is not None and info.running


def _forget_activity(client: TestClient) -> Any:
    async def forget() -> None:
        registry(client).take_activity()

    return forget
