"""Helpers for lab tests that run the API against PostgreSQL."""

import asyncio
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response

from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.runtime import ContainerInfo
from linuxlab.labs.runtime.fake import FakeRuntime, Operation
from linuxlab.labs.runtime.spec import container_name
from tests.auth.support import request, run, session_token, signup, sql
from tests.content.support import FIXTURE

LAB_FIELDS = {"id", "status", "end_reason", "created_at", "expires_at", "ended_at", "mission"}
# A published mission of the synthetic content in tests/fixtures/content.
MISSION = "sample-file"


def user(client: TestClient, email: str) -> str:
    """Sign up a new account and return its session token."""
    return session_token(signup(client, email))


def publish_missions(client: TestClient) -> None:
    """Sync the synthetic content, once per emptied database."""
    if sql(client, "SELECT count(*) FROM missions")[0][0]:
        return
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, load_content(FIXTURE)))


def create_lab(client: TestClient, token: str | None, mission: str = MISSION) -> Response:
    publish_missions(client)
    return request(client, "POST", "/api/labs", token=token, json={"mission_slug": mission})


def end_lab(client: TestClient, token: str | None, lab_id: str) -> Response:
    return request(client, "DELETE", f"/api/labs/{lab_id}", token=token, json={})


def get_lab(client: TestClient, token: str | None, lab_id: str) -> Response:
    return request(client, "GET", f"/api/labs/{lab_id}", token=token)


def current_lab(client: TestClient, token: str | None) -> Any:
    response = request(client, "GET", "/api/labs/current", token=token)
    assert response.status_code == 200, response.text
    return response.json()


def ready_lab(client: TestClient, token: str) -> dict[str, Any]:
    response = create_lab(client, token)
    assert response.status_code == 201, response.text
    lab: dict[str, Any] = response.json()
    assert lab["status"] == "ready"
    return lab


def fake_runtime(client: TestClient) -> FakeRuntime:
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert isinstance(runtime, FakeRuntime)
    return runtime


def labs(client: TestClient) -> Labs:
    service: Labs = client.app.state.labs  # type: ignore[attr-defined]
    return service


def container(client: TestClient, lab_id: str) -> ContainerInfo | None:
    name = container_name(uuid.UUID(lab_id).hex)
    return next((c for c in fake_runtime(client).containers() if c.name == name), None)


def lab_row(client: TestClient, lab_id: str) -> tuple[str, str | None]:
    rows = sql(client, "SELECT status, end_reason FROM lab_sessions WHERE id = :id", id=lab_id)
    assert len(rows) == 1
    return rows[0][0], rows[0][1]


def run_async(client: TestClient, function: Callable[[], Any]) -> Any:
    assert client.portal is not None
    return client.portal.call(function)


def hold(client: TestClient, operation: Operation) -> Callable[[], None]:
    """Hold the runtime operation until the returned function is called."""
    gate = run_async(client, _new_event)
    fake_runtime(client).gates[operation] = gate

    def release() -> None:
        fake_runtime(client).gates.pop(operation, None)
        assert client.portal is not None
        client.portal.call(gate.set)

    return release


def wait_for_call(client: TestClient, operation: Operation, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not any(name == operation for name, _ in fake_runtime(client).calls):
        assert time.monotonic() < deadline, f"runtime never called {operation}"
        time.sleep(0.01)


def in_background(function: Callable[[], Response]) -> "Future[Response]":
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(function)
    executor.shutdown(wait=False)
    return future


async def _new_event() -> asyncio.Event:
    return asyncio.Event()
