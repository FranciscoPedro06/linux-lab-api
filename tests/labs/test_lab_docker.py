"""The lab lifecycle against the real Docker Engine and PostgreSQL.

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc locally, runsc in the
Runtime workflow).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

import aiodocker
import httpx2
import pytest
from aiodocker.exceptions import DockerError

from linuxlab.auth.cookies import SESSION_COOKIE
from linuxlab.labs.lifecycle import NO_TERMINAL_TIMEOUT
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime import LabContainerSpec
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.runtime.spec import container_name

from .app_support import Account, Api, running_api
from .support import LAB_IMAGE, OCI_RUNTIME
from .terminal_client import ORIGIN, close_code, read_until, start, terminal

pytestmark = [pytest.mark.docker, pytest.mark.integration]


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
        # Whatever a test left behind, the reaper of this deployment removes it.
        await api.sql("UPDATE lab_sessions SET last_activity_at = now() - interval '1 day'")
        await api.labs.reconcile(startup=True)


async def docker_state(client: aiodocker.Docker, lab: LabSession) -> dict[str, object] | None:
    try:
        data = await client.containers.container(container_name(lab.lab_key)).show()
    except DockerError as error:
        if error.status == 404:
            return None
        raise
    state: dict[str, object] = data["State"]
    return state


async def shell(api: Api, account: Account, lab: LabSession, command: str, done: str) -> bytes:
    async with terminal(api.url, str(lab.id), account.token) as websocket:
        await start(websocket)
        await read_until(websocket, b"$ ")
        await websocket.send(f"{command}; echo {done}$((1))\r".encode())
        return await read_until(websocket, f"{done}1\r\n".encode())


async def test_created_lab_runs_a_labeled_isolated_container(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")

    lab = await api.lab(ana)

    data = await docker_client.containers.container(container_name(lab.lab_key)).show()
    assert data["State"]["Running"]
    labels = {k: v for k, v in data["Config"]["Labels"].items() if k.startswith("linuxlab.")}
    assert labels == {
        "linuxlab.managed": "true",
        "linuxlab.lab_id": lab.lab_key,
        "linuxlab.deployment": api.app.state.settings.lab_deployment,
    }
    assert data["HostConfig"]["Runtime"] == OCI_RUNTIME
    assert data["HostConfig"]["NetworkMode"] == "none"
    assert data["HostConfig"]["ReadonlyRootfs"] is True
    assert data["Config"]["User"] == "1000:1000"


async def test_full_lifecycle_ends_with_the_container_removed(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    lab = await api.lab(ana)

    output = await shell(api, ana, lab, "pwd; whoami", "done")
    assert b"/home/student\r\n" in output
    assert b"student\r\n" in output

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await api.end(lab)
        assert await close_code(websocket) == 4410

    assert await api.status(lab) == ("terminated", "user")
    assert await docker_state(docker_client, lab) is None


async def test_logout_removes_the_users_container(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    bia = await api.account("bia@example.com")
    lab_a = await api.lab(ana)
    lab_b = await api.lab(bia)

    base_url = api.url.replace("ws://", "http://")
    async with httpx2.AsyncClient(base_url=base_url) as http:
        response = await http.post(
            "/api/auth/logout",
            json={},
            headers={"origin": ORIGIN, "cookie": f"{SESSION_COOKIE}={ana.token}"},
        )
    assert response.status_code == 204

    assert await api.status(lab_a) == ("terminated", "logout")
    assert await docker_state(docker_client, lab_a) is None
    state_b = await docker_state(docker_client, lab_b)
    assert state_b is not None and state_b["Running"]


async def test_container_removed_while_the_terminal_is_connected(
    api: Api, docker_client: aiodocker.Docker, caplog: pytest.LogCaptureFixture
) -> None:
    ana = await api.account("ana@example.com")
    lab = await api.lab(ana)

    with caplog.at_level(logging.WARNING, logger="linuxlab"):
        async with terminal(api.url, str(lab.id), ana.token) as websocket:
            await start(websocket)
            await read_until(websocket, b"$ ")
            await websocket.send(b"trap '' HUP; sleep 371\r")
            await docker_client.containers.container(container_name(lab.lab_key)).delete(force=True)
            assert await close_code(websocket, seconds=20) == 4410

    assert await api.status(lab) == ("terminated", "container_lost")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], [
        r.getMessage() for r in caplog.records
    ]


async def test_container_that_disappeared_is_reconciled(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    lab = await api.lab(ana)
    await docker_client.containers.container(container_name(lab.lab_key)).delete(force=True)

    await api.labs.reconcile()

    assert await api.status(lab) == ("terminated", "container_lost")


async def test_reaper_ends_idle_labs_and_removes_orphans(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    bia = await api.account("bia@example.com")
    idle = await api.lab(ana)
    busy = await api.lab(bia)
    await api.sql(
        "UPDATE lab_sessions SET last_activity_at = now() - CAST(:idle AS interval) WHERE id = :id",
        idle=NO_TERMINAL_TIMEOUT,
        id=idle.id,
    )
    runtime: DockerRuntime = api.app.state.runtime
    deployment = api.app.state.settings.lab_deployment
    orphan = await runtime.create(
        LabContainerSpec(lab_id=uuid.uuid4().hex, image=LAB_IMAGE, deployment=deployment)
    )
    stranger = await runtime.create(
        LabContainerSpec(lab_id=uuid.uuid4().hex, image=LAB_IMAGE, deployment="someone-else")
    )
    try:
        await api.labs.reconcile()

        assert await api.status(idle) == ("terminated", "no_terminal")
        assert await docker_state(docker_client, idle) is None
        assert await api.status(busy) == ("ready", None)
        state = await docker_state(docker_client, busy)
        assert state is not None and state["Running"]
        remaining = {info.id for info in await runtime.list_labs(deployment)}
        assert orphan.id not in remaining
        assert (await runtime.inspect(stranger.id)).name == stranger.name
    finally:
        await runtime.remove(orphan.id)
        await runtime.remove(stranger.id)


async def test_exceeding_memory_is_reported_as_oom(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    """Under gVisor the memory cgroup covers the whole sandbox, so exceeding it ends
    the lab, and the student is told why. Under runc only the process dies."""
    ana = await api.account("ana@example.com")
    lab = await api.lab(ana)
    allocate = "python3 -c \"data = b'x' * (768 * 1024 * 1024)\""

    async with terminal(api.url, str(lab.id), ana.token) as websocket:
        await start(websocket)
        await read_until(websocket, b"$ ")
        await websocket.send(f"{allocate}; echo status=$?\r".encode())
        if OCI_RUNTIME == "runsc":
            assert await close_code(websocket, seconds=60) == 4410
        else:
            assert b"status=137" in await read_until(websocket, b"status=137", seconds=60)

    if OCI_RUNTIME == "runsc":
        assert await api.status(lab) == ("terminated", "oom")
        assert await docker_state(docker_client, lab) is None
    else:
        await api.labs.reconcile()
        assert await api.status(lab) == ("ready", None)


async def test_two_users_labs_are_separate_containers(
    api: Api, docker_client: aiodocker.Docker
) -> None:
    ana = await api.account("ana@example.com")
    bia = await api.account("bia@example.com")
    lab_a = await api.lab(ana)
    lab_b = await api.lab(bia)

    await shell(api, ana, lab_a, "echo secret-a > ~/note", "wrote")
    output = await shell(api, bia, lab_b, "cat ~/note 2>&1", "read")

    assert b"No such file or directory" in output
    assert container_name(lab_a.lab_key) != container_name(lab_b.lab_key)
    await asyncio.gather(api.end(lab_a), api.end(lab_b))
    assert await docker_state(docker_client, lab_a) is None
    assert await docker_state(docker_client, lab_b) is None
