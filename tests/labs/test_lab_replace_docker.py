"""Replacing a lab against the real Docker Engine and PostgreSQL.

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc in CI, runsc in the
Runtime workflow).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import aiodocker
import httpx2
import pytest
from aiodocker.exceptions import DockerError

from linuxlab.auth.cookies import SESSION_COOKIE
from linuxlab.labs.lifecycle import (
    PROVISIONING_TIMEOUT,
    Creation,
    LabStartError,
    PreviousLabNotRemovedError,
    SessionEndedError,
    utcnow,
)
from linuxlab.labs.models import EndReason, LabSession
from linuxlab.labs.runtime import ExecUser, LabRuntimeError
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.runtime.spec import container_name
from tests.content.support import copy_fixture, edit_yaml, mission_file

from .app_support import MISSION, Account, Api, running_api
from .support import LAB_IMAGE, OCI_RUNTIME
from .terminal_client import ORIGIN, close_code, read_until, start, terminal

pytestmark = [pytest.mark.docker, pytest.mark.integration]

OTHER = "sample-answer"


@pytest.fixture
async def api(docker_client: aiodocker.Docker) -> AsyncIterator[Api]:
    runtime = DockerRuntime(docker_client, oci_runtime=OCI_RUNTIME)
    async with running_api(
        runtime,
        lab_oci_runtime=OCI_RUNTIME,
        lab_image=LAB_IMAGE,
        lab_deployment=f"tests-{uuid.uuid4().hex[:12]}",
    ) as api:
        yield api
        # Every lab still running is ended explicitly: a terminal used during the test
        # counts as recent activity, which the reaper would store before any timeout.
        for (lab_id,) in await api.sql(
            "SELECT id FROM lab_sessions WHERE status IN ('provisioning', 'ready')"
        ):
            await api.labs.end(lab_id, EndReason.USER)
        await api.labs.reconcile(startup=True)
        assert await deployment_containers(api) == set()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


def admit() -> None:
    pass


async def replace_as(
    api: Api, account: Account, lab: LabSession, mission: str = MISSION
) -> Creation:
    return await api.labs.replace(lab.id, mission, session_id=account.session_id, admit=admit)


async def replace(
    api: Api, account: Account, lab: LabSession, mission: str = MISSION
) -> LabSession:
    creation = await replace_as(api, account, lab, mission)
    assert creation.created
    return creation.lab


async def container_info(client: aiodocker.Docker, lab: LabSession) -> dict[str, object] | None:
    try:
        data: dict[str, object] = await client.containers.container(
            container_name(lab.lab_key)
        ).show()
    except DockerError as error:
        if error.status == 404:
            return None
        raise
    return data


async def deployment_containers(api: Api) -> set[str]:
    runtime: DockerRuntime = api.app.state.runtime
    listed = await runtime.list_labs(api.app.state.settings.lab_deployment)
    return {info.labels["linuxlab.lab_id"] for info in listed}


async def run_in(api: Api, lab: LabSession, command: str, user: ExecUser = "student") -> str:
    runtime: DockerRuntime = api.app.state.runtime
    info = await runtime.inspect(container_name(lab.lab_key))
    result = await runtime.exec(info.id, ["sh", "-c", command], user=user, time_limit=20)
    return result.stdout.decode()


async def setup_running(api: Api, lab: LabSession) -> bool:
    try:
        # The bracket keeps pgrep from matching its own shell's command line.
        return "sleep" in await run_in(api, lab, "pgrep -a -f 'slee[p] 30' || true", "root")
    except LabRuntimeError:
        # Not started yet.
        return False


async def leave_traces(api: Api, account: Account, lab: LabSession) -> None:
    """Files written from the terminal, like a student would, and a background process
    that outlives the command that started it."""
    async with terminal(api.url, str(lab.id), account.token) as websocket:
        await start(websocket)
        await read_until(websocket, b"$ ")
        await websocket.send(
            b"echo trace > ~/trace.txt; echo trace > /tmp/trace.txt; echo written$((1))\r"
        )
        await read_until(websocket, b"written1\r\n")
    await run_in(api, lab, "nohup sleep 987 >/dev/null 2>&1 &")
    assert await run_in(api, lab, "cat ~/trace.txt /tmp/trace.txt") == "trace\ntrace\n"
    assert await run_in(api, lab, "pgrep -f 'slee[p] 987' || true") != ""


async def test_reset_removes_the_container_and_starts_a_clean_one(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    await leave_traces(api, ana, first)
    before = await container_info(docker_client, first)
    assert before is not None

    second = await replace(api, ana, first)

    assert await api.status(first) == ("terminated", "reset")
    assert await container_info(docker_client, first) is None
    after = await container_info(docker_client, second)
    assert after is not None
    assert after["Id"] != before["Id"]
    assert after["Name"] == f"/{container_name(second.lab_key)}"
    config = after["Config"]
    assert isinstance(config, dict)
    labels = {k: v for k, v in config["Labels"].items() if k.startswith("linuxlab.")}
    assert labels == {
        "linuxlab.managed": "true",
        "linuxlab.lab_id": second.lab_key,
        "linuxlab.deployment": api.app.state.settings.lab_deployment,
    }
    # Nothing of the previous lab comes back: not its files, not its processes.
    assert await run_in(api, second, "ls -A ~/trace.txt /tmp/trace.txt 2>/dev/null || true") == ""
    assert await run_in(api, second, "pgrep -f 'slee[p] 987' || true") == ""
    assert await deployment_containers(api) == {second.lab_key}


async def test_switch_starts_the_other_missions_lab(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)

    second = await replace(api, ana, first, OTHER)

    assert await api.status(first) == ("terminated", "mission_switch")
    assert await container_info(docker_client, first) is None
    assert await deployment_containers(api) == {second.lab_key}


async def test_the_old_terminal_is_closed_and_refused_and_the_new_one_is_separate(
    api: Api,
) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    await leave_traces(api, ana, first)

    async with terminal(api.url, str(first.id), ana.token) as websocket:
        await start(websocket)
        second = await replace(api, ana, first)
        assert await close_code(websocket) == 4410

    async with terminal(api.url, str(first.id), ana.token) as websocket:
        assert await close_code(websocket) == 4410

    async with terminal(api.url, str(second.id), ana.token) as websocket:
        await start(websocket)
        await read_until(websocket, b"$ ")
        await websocket.send(b"cat ~/trace.txt 2>&1; echo done$((1))\r")
        output = await read_until(websocket, b"done1\r\n")
    assert b"No such file" in output

    # The new lab is its owner's only: another user's session cannot open it.
    bia = await api.account("bia@example.com")
    async with terminal(api.url, str(second.id), bia.token) as websocket:
        assert await close_code(websocket) == 4404


async def test_failed_removal_is_recovered(api: Api, docker_client: aiodocker.Docker) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    runtime: DockerRuntime = api.app.state.runtime
    real_remove = runtime.remove

    async def broken_remove(container_id: str) -> None:
        raise LabRuntimeError(f"{container_id}: synthetic removal failure")

    runtime.remove = broken_remove  # type: ignore[method-assign]
    try:
        with pytest.raises(PreviousLabNotRemovedError):
            await replace_as(api, ana, first)
    finally:
        runtime.remove = real_remove  # type: ignore[method-assign]

    assert await api.status(first) == ("terminating", "reset")
    state = await container_info(docker_client, first)
    assert state is not None
    # The reaper's startup pass, as after a restart, removes it; asking again resumes.
    await api.labs.reconcile(startup=True)
    assert await api.status(first) == ("terminated", "reset")
    assert await container_info(docker_client, first) is None

    second = await replace(api, ana, first)
    assert await deployment_containers(api) == {second.lab_key}


async def test_setup_timeout_of_the_successor(
    api: Api, root: Path, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    (root / "missions" / OTHER / "setup.sh").write_text("sleep 60\n", encoding="utf-8")
    edit_yaml(mission_file(root, OTHER), lambda data: data["setup"].update(timeout_seconds=2))
    await api.sync(root)

    with pytest.raises(LabStartError):
        await replace_as(api, ana, first, OTHER)

    assert await api.status(first) == ("terminated", "mission_switch")
    rows = await api.sql(
        "SELECT id, status, end_reason FROM lab_sessions WHERE previous_lab_id = :id", id=first.id
    )
    assert [(status, reason) for _, status, reason in rows] == [
        ("terminated", "provisioning_failed")
    ]
    assert await deployment_containers(api) == set()


async def test_api_stopping_during_the_successors_setup_leaves_no_orphan(
    api: Api, root: Path
) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    (root / "missions" / MISSION / "setup.sh").write_text("sleep 30\n", encoding="utf-8")
    await api.sync(root)

    # A shutdown cancels the request while the successor's setup runs.
    replacing = asyncio.create_task(replace_as(api, ana, first))
    async with asyncio.timeout(30):
        while True:
            rows = await api.sql(
                "SELECT id FROM lab_sessions WHERE previous_lab_id = :id", id=first.id
            )
            if rows and await deployment_containers(api):
                break
            await asyncio.sleep(0.1)
    successor = await api.labs.get(rows[0][0])
    assert successor is not None
    async with asyncio.timeout(30):
        while not await setup_running(api, successor):
            await asyncio.sleep(0.1)
    replacing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacing
    assert await api.status(successor) == ("provisioning", None)

    # The next process's reaper fails it once the provisioning timeout has passed.
    later = utcnow() + PROVISIONING_TIMEOUT + timedelta(seconds=1)
    await api.labs.reconcile(startup=True, now=later)

    assert await api.status(successor) == ("terminated", "provisioning_timeout")
    assert await api.status(first) == ("terminated", "reset")
    assert await deployment_containers(api) == set()


async def http_logout(api: Api, account: Account) -> int:
    base_url = api.url.replace("ws://", "http://")
    async with httpx2.AsyncClient(base_url=base_url) as http:
        response = await http.post(
            "/api/auth/logout",
            json={},
            headers={"origin": ORIGIN, "cookie": f"{SESSION_COOKIE}={account.token}"},
        )
    return response.status_code


async def session_rows(api: Api, account: Account) -> int:
    rows = await api.sql("SELECT count(*) FROM auth_sessions WHERE id = :id", id=account.session_id)
    count: int = rows[0][0]
    return count


async def test_logout_before_the_successor_is_inserted_leaves_no_lab(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    original = api.labs._create_successor

    async def logout_first(*args: object) -> Creation:
        assert await http_logout(api, ana) == 204
        return await original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(api.labs, "_create_successor", logout_first)
    with pytest.raises(SessionEndedError):
        await replace_as(api, ana, first)

    assert await session_rows(api, ana) == 0
    assert await api.status(first) == ("terminated", "reset")
    assert (
        await api.sql("SELECT id FROM lab_sessions WHERE previous_lab_id = :id", id=first.id) == []
    )
    assert await deployment_containers(api) == set()


async def test_logout_during_the_successors_setup_ends_it(api: Api, root: Path) -> None:
    ana = await api.account("ana@example.com")
    first = await api.lab(ana)
    (root / "missions" / MISSION / "setup.sh").write_text("sleep 30\n", encoding="utf-8")
    await api.sync(root)

    replacing = asyncio.create_task(replace_as(api, ana, first))
    async with asyncio.timeout(30):
        while True:
            rows = await api.sql(
                "SELECT id FROM lab_sessions WHERE previous_lab_id = :id", id=first.id
            )
            if rows and await deployment_containers(api):
                break
            await asyncio.sleep(0.1)
    successor = await api.labs.get(rows[0][0])
    assert successor is not None
    async with asyncio.timeout(30):
        while not await setup_running(api, successor):
            await asyncio.sleep(0.1)

    assert await http_logout(api, ana) == 204

    # Answered: the successor has been ended and its container removed.
    assert await session_rows(api, ana) == 0
    assert await api.status(successor) == ("terminated", "logout")
    assert await deployment_containers(api) == set()
    # The replacement answers with the successor as it read it once setup was cut
    # short: ended, possibly while logout was still removing it.
    creation = await replacing
    assert creation.lab.id == successor.id
    assert creation.lab.end_reason == "logout"
    assert await api.status(successor) == ("terminated", "logout")
    assert await deployment_containers(api) == set()
