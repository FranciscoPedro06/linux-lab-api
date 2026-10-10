"""Labs created for missions: version pinning, parameters, setup and failures.

PostgreSQL with labs on a FakeRuntime: exec calls are recorded, not run. Docker tests
(test_lab_docker.py) run the same flow in real containers.
"""

import asyncio
import json
import logging
import shutil
import time
import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from linuxlab.content import params as content_params
from linuxlab.content.loader import load_content
from linuxlab.content.params import ParamError
from linuxlab.content.sync import sync_content
from linuxlab.labs import lifecycle
from linuxlab.labs.lifecycle import LABCTL_INIT, PROVISIONING_TIMEOUT, SETUP_COMMAND
from linuxlab.labs.runtime import ExecResult, ExecUser, LabRuntimeError
from linuxlab.labs.runtime.fake import ExecCall, FakeRuntime
from tests.auth.support import request, run, sql
from tests.conftest import AppFactory
from tests.content.support import FIXTURE, copy_fixture, edit_yaml, mission_file, module_file

from .lab_support import (
    MISSION,
    container,
    create_lab,
    current_lab,
    end_lab,
    fake_runtime,
    get_lab,
    hold,
    in_background,
    lab_row,
    labs,
    publish_missions,
    ready_lab,
    run_async,
    user,
    wait_for_call,
)

pytestmark = pytest.mark.integration

SETUP_SCRIPT = (FIXTURE / "missions" / MISSION / "setup.sh").read_text(encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A copy of the synthetic content that a test can change and sync."""
    return copy_fixture(tmp_path)


def sync(client: TestClient, root: Path) -> None:
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, load_content(root)))


def pinned(client: TestClient, lab_id: str) -> tuple[str, int, dict[str, str]]:
    rows = sql(
        client,
        "SELECT m.slug, l.mission_version, l.params FROM lab_sessions l"
        " JOIN missions m ON m.id = l.mission_id WHERE l.id = :id",
        id=lab_id,
    )
    assert len(rows) == 1
    slug, version, params = rows[0]
    return slug, version, params


def setup_calls(client: TestClient) -> list[ExecCall]:
    return [call for call in fake_runtime(client).exec_calls if call.argv == SETUP_COMMAND]


def mission_detail(client: TestClient, token: str, slug: str = MISSION) -> Any:
    return request(client, "GET", f"/api/missions/{slug}", token=token)


class SlowSetup(FakeRuntime):
    """Setup is recorded, then blocks until `setup_gate` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.setup_started = asyncio.Event()
        self.setup_gate = asyncio.Event()

    async def exec(self, container_id: str, argv: Sequence[str], **kwargs: Any) -> ExecResult:
        result = await super().exec(container_id, argv, **kwargs)
        if tuple(argv) == SETUP_COMMAND:
            self.setup_started.set()
            await self.setup_gate.wait()
        return result


def wait_for_setup(runtime: SlowSetup, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not runtime.setup_started.is_set():
        assert time.monotonic() < deadline, "setup never started"
        time.sleep(0.01)


def failing(command: Sequence[str], result: ExecResult) -> Any:
    def handler(argv: Sequence[str], user: ExecUser) -> ExecResult:
        if tuple(argv) == tuple(command):
            return result
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    return handler


# Creation


def test_lab_is_pinned_to_the_current_version_with_its_parameters(client: TestClient) -> None:
    ana = user(client, "ana@example.com")

    response = create_lab(client, ana)

    assert response.status_code == 201
    lab = response.json()
    assert lab["status"] == "ready"
    assert lab["mission"] == {"slug": MISSION, "title": "Arquivo de teste", "version": 1}
    slug, version, params = pinned(client, lab["id"])
    assert (slug, version) == (MISSION, 1)
    assert set(params) == {"token"}
    assert isinstance(params["token"], str)
    assert len(params["token"]) == 8
    assert params["token"] == params["token"].lower()
    int(params["token"], 16)


def test_parameters_never_reach_the_client(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    token = pinned(client, lab["id"])[2]["token"]

    responses = [
        create_lab(client, ana),
        get_lab(client, ana, lab["id"]),
        request(client, "GET", "/api/labs", token=ana),
        request(client, "GET", "/api/labs/current", token=ana),
        mission_detail(client, ana),
        request(client, "GET", "/api/modules", token=ana),
        end_lab(client, ana, lab["id"]),
    ]

    for response in responses:
        assert response.status_code == 200, response.text
        assert token not in response.text
        assert "params" not in response.text
        assert "LAB_PARAM" not in response.text


def test_container_is_prepared_before_the_lab_is_ready(client: TestClient) -> None:
    ana = user(client, "ana@example.com")

    lab = ready_lab(client, ana)

    info = container(client, lab["id"])
    assert info is not None
    token = pinned(client, lab["id"])[2]["token"]
    assert fake_runtime(client).exec_calls == [
        ExecCall(info.id, LABCTL_INIT, "root", lifecycle.LABCTL_INIT_SECONDS),
        ExecCall(
            info.id,
            SETUP_COMMAND,
            "student",
            20,
            SETUP_SCRIPT.encode("utf-8"),
            {"LAB_PARAM_TOKEN": token},
        ),
    ]


def test_lab_is_not_ready_during_init(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    release = hold(client, "exec")

    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "exec")

    assert current_lab(client, ana)["status"] == "provisioning"
    assert setup_calls(client) == []
    release()
    assert creating.result(10).json()["status"] == "ready"


def test_lab_is_not_ready_during_setup(make_client: AppFactory) -> None:
    runtime = SlowSetup()
    client = make_client(runtime=runtime)
    ana = user(client, "ana@example.com")
    publish_missions(client)

    creating = in_background(lambda: create_lab(client, ana))
    wait_for_setup(runtime)

    assert current_lab(client, ana)["status"] == "provisioning"
    assert len(setup_calls(client)) == 1
    run_async(client, runtime.setup_gate.set)
    assert creating.result(10).json()["status"] == "ready"


def test_setup_runs_as_the_user_of_the_version(make_client: AppFactory, root: Path) -> None:
    client = make_client()
    edit_yaml(
        mission_file(root, MISSION),
        lambda data: data["setup"].update(user="root", timeout_seconds=7),
    )
    sync(client, root)

    ready_lab(client, user(client, "ana@example.com"))

    (call,) = setup_calls(client)
    assert (call.user, call.time_limit) == ("root", 7)


def test_mission_without_parameters_gets_an_empty_object(client: TestClient, root: Path) -> None:
    def drop_params(data: dict[str, Any]) -> None:
        del data["params"]
        conditions = data["validation"]["all"]
        data["validation"]["all"] = [c for c in conditions if c["id"] != "content-kept"]

    edit_yaml(mission_file(root, MISSION), drop_params)
    sync(client, root)

    lab = ready_lab(client, user(client, "ana@example.com"))

    assert pinned(client, lab["id"])[2] == {}
    assert setup_calls(client)[0].env == {}


def test_each_lab_gets_its_own_parameters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    generated: list[dict[str, str]] = []
    original = content_params.generate_params

    def counting(*args: Any, **kwargs: Any) -> dict[str, str]:
        values = original(*args, **kwargs)
        generated.append(values)
        return values

    monkeypatch.setattr(lifecycle, "generate_params", counting)
    ana = ready_lab(client, user(client, "ana@example.com"))
    bia = ready_lab(client, user(client, "bia@example.com"))

    assert len(generated) == 2
    assert pinned(client, ana["id"])[2] == generated[0]
    assert pinned(client, bia["id"])[2] == generated[1]
    calls = setup_calls(client)
    assert [call.env for call in calls] == [
        {"LAB_PARAM_TOKEN": generated[0]["token"]},
        {"LAB_PARAM_TOKEN": generated[1]["token"]},
    ]
    assert calls[0].container_id != calls[1].container_id


# Missions that cannot start a lab


@pytest.mark.parametrize(
    "slug",
    ["missing", "sample-draft", "Sample-File", "../sample-file", "a" * 65, "sample file", ""],
)
def test_only_published_missions_start_labs(client: TestClient, slug: str) -> None:
    ana = user(client, "ana@example.com")

    response = create_lab(client, ana, slug)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mission_not_found"
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]
    assert fake_runtime(client).calls == []


def test_empty_catalog_starts_no_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")

    response = request(client, "POST", "/api/labs", token=ana, json={"mission_slug": MISSION})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mission_not_found"
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]
    assert fake_runtime(client).calls == []


def test_archived_mission_and_unpublished_module_start_no_lab(
    client: TestClient, root: Path
) -> None:
    edit_yaml(module_file(root, "alpha"), lambda data: data["missions"].remove("sample-answer"))
    shutil.rmtree(root / "missions" / "sample-answer")
    edit_yaml(module_file(root, "beta"), lambda data: data.update(status="draft"))
    edit_yaml(mission_file(root, "sample-draft"), lambda data: data.update(status="published"))
    sync(client, root)
    ana = user(client, "ana@example.com")

    for slug in ("sample-answer", "sample-draft"):
        response = create_lab(client, ana, slug)
        assert response.status_code == 404, slug
        assert response.json()["error"]["code"] == "mission_not_found"
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]


# An active lab


def test_same_mission_returns_the_lab_without_preparing_it_again(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)
    params = pinned(client, first["id"])[2]

    again = create_lab(client, ana)

    assert again.status_code == 200
    assert again.json() == first
    assert len(setup_calls(client)) == 1
    assert len(fake_runtime(client).containers()) == 1
    assert pinned(client, first["id"])[2] == params


def test_another_mission_is_refused_while_a_lab_is_active(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    first = ready_lab(client, ana)

    response = create_lab(client, ana, "sample-answer")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "active_lab_for_different_mission"
    assert lab_row(client, first["id"]) == ("ready", None)
    assert current_lab(client, ana) == first
    assert len(fake_runtime(client).containers()) == 1
    assert len(setup_calls(client)) == 1


def test_another_mission_is_refused_while_a_lab_is_provisioning(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    release = hold(client, "start")
    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "start")

    other = create_lab(client, ana, "sample-answer")
    same = create_lab(client, ana)
    release()

    assert other.status_code == 409
    assert other.json()["error"]["code"] == "active_lab_for_different_mission"
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "lab_provisioning"
    assert creating.result(10).status_code == 201
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(1,)]


def test_a_lab_being_ended_blocks_any_mission(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    fake_runtime(client).fail.add("remove")
    end_lab(client, ana, lab["id"])

    response = create_lab(client, ana, "sample-answer")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_terminating"


def test_a_lab_without_a_mission_counts_as_another_mission(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    user_id = sql(client, "SELECT id FROM users")[0][0]
    sql(
        client,
        "INSERT INTO lab_sessions (id, user_id, status, created_at, last_activity_at, expires_at)"
        " VALUES (:id, :user_id, 'ready', now(), now(), now() + interval '2 hours')",
        id=uuid.uuid4(),
        user_id=user_id,
    )

    response = create_lab(client, ana)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "active_lab_for_different_mission"
    assert current_lab(client, ana)["mission"] is None


def test_concurrent_creations_prepare_one_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    release = hold(client, "exec")

    first = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "exec")
    second = create_lab(client, ana)
    third = create_lab(client, ana, "sample-answer")
    release()

    assert first.result(10).status_code == 201
    assert second.json()["error"]["code"] == "lab_provisioning"
    assert third.json()["error"]["code"] == "active_lab_for_different_mission"
    assert len(setup_calls(client)) == 1
    assert len(fake_runtime(client).containers()) == 1


# Failures


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(exit_code=1, stdout=b"", stderr=b"boom"),
        ExecResult(exit_code=124, stdout=b"", stderr=b"", timed_out=True),
        ExecResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True),
    ],
)
def test_failed_setup_fails_the_lab(client: TestClient, result: ExecResult) -> None:
    ana = user(client, "ana@example.com")
    fake_runtime(client).exec_handler = failing(SETUP_COMMAND, result)

    response = create_lab(client, ana)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_start_failed"
    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("terminated", "provisioning_failed")
    ]
    assert fake_runtime(client).containers() == []
    assert current_lab(client, ana) is None
    assert len(setup_calls(client)) == 1


def test_connection_that_cannot_half_close_fails_the_lab(client: TestClient) -> None:
    """DockerRuntime refuses setup before sending it; the lab fails and is cleaned up."""
    ana = user(client, "ana@example.com")

    def refuse_setup(argv: Sequence[str], user: ExecUser) -> ExecResult:
        if tuple(argv) == SETUP_COMMAND:
            raise LabRuntimeError(
                "the Docker connection cannot close standard input separately; nothing was sent"
            )
        return ExecResult(exit_code=0, stdout=b"", stderr=b"")

    fake_runtime(client).exec_handler = refuse_setup

    response = create_lab(client, ana)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_start_failed"
    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("terminated", "provisioning_failed")
    ]
    assert fake_runtime(client).containers() == []
    assert current_lab(client, ana) is None


def test_failed_init_skips_setup(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    fake_runtime(client).exec_handler = failing(
        LABCTL_INIT, ExecResult(exit_code=1, stdout=b"", stderr=b"")
    )

    response = create_lab(client, ana)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "lab_start_failed"
    assert setup_calls(client) == []
    assert fake_runtime(client).containers() == []


def test_runtime_failure_during_setup_fails_the_lab(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    fake_runtime(client).fail.add("exec")

    response = create_lab(client, ana)

    assert response.status_code == 503
    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("terminated", "provisioning_failed")
    ]
    assert fake_runtime(client).containers() == []


def test_failed_removal_after_setup_leaves_the_lab_to_the_reaper(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    runtime = fake_runtime(client)
    runtime.exec_handler = failing(SETUP_COMMAND, ExecResult(exit_code=2, stdout=b"", stderr=b""))
    runtime.fail.add("remove")

    response = create_lab(client, ana)

    assert response.status_code == 503
    assert sql(client, "SELECT status, end_reason FROM lab_sessions") == [
        ("failed", "provisioning_failed")
    ]
    runtime.fail.clear()
    run(client, lambda: labs(client).reconcile(startup=True))
    assert sql(client, "SELECT status FROM lab_sessions") == [("terminated",)]
    assert runtime.containers() == []
    assert len(setup_calls(client)) == 1


def test_parameter_failure_creates_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> dict[str, str]:
        raise ParamError("parameter token: generated value has an invalid format")

    monkeypatch.setattr(lifecycle, "generate_params", broken)
    ana = user(client, "ana@example.com")

    response = create_lab(client, ana)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Erro interno. Tente novamente."}
    }
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]
    assert fake_runtime(client).calls == []


def test_an_unusable_stored_version_creates_nothing(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    # A version the sync could not have written: an unknown generator.
    (mission_id, spec) = sql(
        client,
        "SELECT v.mission_id, v.spec FROM mission_versions v"
        " JOIN missions m ON m.id = v.mission_id WHERE m.slug = :slug",
        slug=MISSION,
    )[0]
    spec["params"]["token"] = {"generator": "uuid"}
    sql(
        client,
        "INSERT INTO mission_versions (mission_id, version, content_hash, spec)"
        " VALUES (:id, 2, :hash, CAST(:spec AS jsonb))",
        id=mission_id,
        hash="0" * 64,
        spec=json.dumps(spec),
    )
    sql(client, "UPDATE missions SET current_version = 2 WHERE id = :id", id=mission_id)

    response = create_lab(client, ana)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert sql(client, "SELECT count(*) FROM lab_sessions") == [(0,)]
    assert fake_runtime(client).calls == []


def test_logs_carry_no_parameter_script_or_output(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ana = user(client, "ana@example.com")
    secret_output = b"OUTPUT-THAT-MUST-NOT-BE-LOGGED"
    fake_runtime(client).exec_handler = failing(
        SETUP_COMMAND, ExecResult(exit_code=3, stdout=secret_output, stderr=secret_output)
    )
    failed = create_lab(client, ana)
    fake_runtime(client).exec_handler = None
    lab = ready_lab(client, ana)
    end_lab(client, ana, lab["id"])

    assert failed.status_code == 503
    tokens = [row[0]["token"] for row in sql(client, "SELECT params FROM lab_sessions")]
    assert len(tokens) == 2
    messages = [record.getMessage() for record in caplog.records]
    assert any("lab setup finished" in message and "exit=3" in message for message in messages)
    for message in messages:
        for token in tokens:
            assert token not in message
        assert "OUTPUT-THAT-MUST-NOT-BE-LOGGED" not in message
        assert "LAB_PARAM" not in message
        assert "TOKEN=" not in message
        assert SETUP_SCRIPT.strip().splitlines()[0] not in message


# Interruptions


def test_lab_ended_during_setup_never_becomes_ready(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    publish_missions(client)
    release = hold(client, "exec")

    creating = in_background(lambda: create_lab(client, ana))
    wait_for_call(client, "exec")
    lab_id = current_lab(client, ana)["id"]
    ended = end_lab(client, ana, lab_id)
    release()
    created = creating.result(10)

    assert ended.status_code == 200
    assert created.status_code == 201
    assert created.json()["status"] == "terminated"
    assert lab_row(client, lab_id) == ("terminated", "user")
    assert fake_runtime(client).containers() == []


def test_interrupted_provisioning_is_failed_by_the_reaper_without_setup_again(
    make_client: AppFactory,
) -> None:
    """The request that runs setup dies (cancelled, or the API crashed) mid-setup."""
    runtime = SlowSetup()
    client = make_client(runtime=runtime)
    ana = user(client, "ana@example.com")
    publish_missions(client)
    user_id = sql(client, "SELECT id FROM users")[0][0]

    async def cancel_during_setup() -> None:
        task = asyncio.create_task(labs(client).create(user_id, MISSION))
        await runtime.setup_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_async(client, cancel_during_setup)
    lab = current_lab(client, ana)
    assert lab["status"] == "provisioning"
    assert len(setup_calls(client)) == 1
    exec_calls = len(runtime.exec_calls)

    # Before the provisioning timeout the reaper leaves it alone, even at startup.
    run(client, lambda: labs(client).reconcile(startup=True))
    assert lab_row(client, lab["id"]) == ("provisioning", None)

    later = lifecycle.utcnow() + PROVISIONING_TIMEOUT + timedelta(seconds=1)
    run(client, lambda: labs(client).reconcile(now=later))

    assert lab_row(client, lab["id"]) == ("terminated", "provisioning_timeout")
    assert runtime.containers() == []
    assert len(runtime.exec_calls) == exec_calls
    assert len(setup_calls(client)) == 1
