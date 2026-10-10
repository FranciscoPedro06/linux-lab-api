"""Mission setup in real containers: what a lab looks like when it becomes ready.

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc locally, runsc in the
Runtime workflow), with PostgreSQL.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import aiodocker
import pytest
from aiodocker.exceptions import DockerError

from linuxlab.labs.lifecycle import LabStartError
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime import ExecResult, ExecUser
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.runtime.spec import container_name
from tests.content.support import copy_fixture, edit_yaml, mission_file

from .app_support import MISSION, Api, running_api
from .support import LAB_IMAGE, OCI_RUNTIME

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
        await api.sql("UPDATE lab_sessions SET last_activity_at = now() - interval '1 day'")
        await api.labs.reconcile(startup=True)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


async def with_setup(
    api: Api, root: Path, script: str, *, user: ExecUser = "student", seconds: int = 20
) -> None:
    """Publish a new version of the mission with this setup script."""
    (root / "missions" / MISSION / "setup.sh").write_text(script, encoding="utf-8")
    edit_yaml(
        mission_file(root, MISSION),
        lambda data: data["setup"].update(user=user, timeout_seconds=seconds),
    )
    await api.sync(root)


async def run_in(api: Api, lab: LabSession, command: str, user: ExecUser = "student") -> ExecResult:
    runtime: DockerRuntime = api.app.state.runtime
    info = await runtime.inspect(container_name(lab.lab_key))
    return await runtime.exec(info.id, ["sh", "-c", command], user=user, time_limit=20)


async def output(api: Api, lab: LabSession, command: str, user: ExecUser = "student") -> str:
    result = await run_in(api, lab, command, user)
    assert result.exit_code == 0, (command, result.stderr)
    return result.stdout.decode()


async def params(api: Api, lab: LabSession) -> dict[str, str]:
    rows = await api.sql("SELECT params FROM lab_sessions WHERE id = :id", id=lab.id)
    value: dict[str, str] = rows[0][0]
    return value


async def container_exists(client: aiodocker.Docker, lab_key: str) -> bool:
    try:
        await client.containers.container(container_name(lab_key)).show()
    except DockerError as error:
        if error.status == 404:
            return False
        raise
    return True


async def failed_lab(api: Api, docker_client: aiodocker.Docker, email: str) -> LabSession:
    """Start a lab that must fail; return its row once the attempt is over."""
    account = await api.account(email)
    with pytest.raises(LabStartError):
        await api.labs.create(account.id, MISSION)
    rows = await api.sql("SELECT id FROM lab_sessions WHERE user_id = :id", id=account.id)
    lab = await api.labs.get(rows[0][0])
    assert lab is not None
    assert (lab.status, lab.end_reason) == ("terminated", "provisioning_failed")
    assert not await container_exists(docker_client, lab.lab_key)
    return lab


# A ready lab


async def test_setup_prepares_the_lab_with_its_parameters(api: Api) -> None:
    lab = await api.lab(await api.account("ana@example.com"))

    token = (await params(api, lab))["token"]
    assert await output(api, lab, "cat ~/sample.sh") == f"TOKEN={token}\n"
    assert await output(api, lab, "stat -c '%U %G %a' ~/sample.sh") == "student student 644\n"
    # labctl init ran first.
    assert await output(api, lab, "cmp ~/.bashrc /etc/skel/.bashrc && echo same") == "same\n"


async def test_setup_script_is_not_written_to_the_lab(api: Api, root: Path) -> None:
    marker = uuid.uuid4().hex
    await with_setup(api, root, f": {marker}\necho done > ~/setup-ran\n")

    lab = await api.lab(await api.account("ana@example.com"))

    assert await output(api, lab, "cat ~/setup-ran") == "done\n"
    found = await run_in(
        api,
        lab,
        f"grep -rl {marker} /home /tmp /run/lab; true",
        user="root",
    )
    assert found.stdout == b""


async def test_setup_runs_as_the_student_without_privileges(api: Api, root: Path) -> None:
    await with_setup(
        api,
        root,
        "id -u > ~/uid\n"
        "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status | tr -s '\\t' ' ' > ~/status\n"
        "pwd > ~/cwd\n"
        "env | cut -d= -f1 | sort > ~/env\n",
    )

    lab = await api.lab(await api.account("ana@example.com"))

    assert await output(api, lab, "cat ~/uid") == "1000\n"
    assert await output(api, lab, "cat ~/status") == "CapEff: 0000000000000000\nNoNewPrivs: 1\n"
    assert await output(api, lab, "cat ~/cwd") == "/home/student\n"
    names = set((await output(api, lab, "cat ~/env")).split())
    assert "LAB_PARAM_TOKEN" in names
    assert names <= {"PATH", "LANG", "HOSTNAME", "HOME", "LAB_PARAM_TOKEN", "PWD", "SHLVL", "_"}


async def test_root_setup_writes_only_where_the_lab_allows(api: Api, root: Path) -> None:
    await with_setup(
        api,
        root,
        "id -u > /run/lab/uid\n"
        "pwd > /tmp/cwd\n"
        "install -o root -g root -m 600 /dev/null /home/student/root-only\n"
        "echo x > /tmp/shared\n"
        "if touch /etc/linuxlab-setup 2>/dev/null; then echo writable; else echo ro; fi"
        " > /tmp/rootfs\n",
        user="root",
    )

    lab = await api.lab(await api.account("ana@example.com"))

    assert await output(api, lab, "cat /run/lab/uid") == "0\n"
    # Root setup runs from /, with root's HOME: missions write to absolute paths.
    assert await output(api, lab, "cat /tmp/cwd") == "/\n"
    assert await output(api, lab, "stat -c '%U %a' ~/root-only") == "root 600\n"
    assert (await run_in(api, lab, "cat ~/root-only")).exit_code != 0
    assert await output(api, lab, "cat /tmp/rootfs") == "ro\n"
    # The student still has no way to root.
    assert await output(api, lab, "id -u") == "1000\n"


async def test_large_setup_output_is_drained(api: Api, root: Path) -> None:
    await with_setup(
        api,
        root,
        "head -c 3000000 /dev/zero | tr '\\0' x\n"
        "head -c 3000000 /dev/zero | tr '\\0' y >&2\n"
        "echo finished > ~/done\n",
    )

    lab = await api.lab(await api.account("ana@example.com"))

    assert await output(api, lab, "cat ~/done") == "finished\n"


async def test_background_process_started_by_setup_has_no_parameters(api: Api, root: Path) -> None:
    await with_setup(api, root, "env -i sleep 600 >/dev/null 2>&1 &\necho ok > ~/started\n")
    started = time.monotonic()

    lab = await api.lab(await api.account("ana@example.com"))

    assert time.monotonic() - started < 15
    assert await output(api, lab, "cat ~/started") == "ok\n"
    pid = (await output(api, lab, "pgrep -f 'sleep 600'")).split()[0]
    environ = await output(api, lab, f"tr '\\0' '\\n' < /proc/{pid}/environ; echo end")
    assert "LAB_PARAM" not in environ
    leaked = await output(api, lab, "grep -l LAB_PARAM /proc/[0-9]*/environ 2>/dev/null; true")
    assert leaked == ""


# Failures


async def test_failing_setup_fails_the_lab_and_removes_the_container(
    api: Api, root: Path, docker_client: aiodocker.Docker, caplog: pytest.LogCaptureFixture
) -> None:
    await with_setup(api, root, 'echo "secret $LAB_PARAM_TOKEN"\necho "secret" >&2\nexit 3\n')

    with caplog.at_level(logging.INFO, logger="linuxlab"):
        lab = await failed_lab(api, docker_client, "ana@example.com")

    messages = [record.getMessage() for record in caplog.records]
    assert any(f"lab={lab.id} exit=3 timed_out=False" in message for message in messages)
    token = (await params(api, lab))["token"]
    assert not any(token in message or "secret" in message for message in messages)


async def test_an_unset_variable_fails_setup(
    api: Api, root: Path, docker_client: aiodocker.Docker
) -> None:
    # bash runs with -u: a typo in a parameter name is an error, not an empty string.
    await with_setup(api, root, 'echo "$LAB_PARAM_TOKN" > ~/x\n')

    await failed_lab(api, docker_client, "ana@example.com")


async def test_setup_timeout_fails_the_lab(
    api: Api, root: Path, docker_client: aiodocker.Docker, caplog: pytest.LogCaptureFixture
) -> None:
    await with_setup(api, root, "sleep 60\n", seconds=2)
    started = time.monotonic()

    with caplog.at_level(logging.INFO, logger="linuxlab"):
        lab = await failed_lab(api, docker_client, "ana@example.com")

    assert time.monotonic() - started < 30
    messages = [record.getMessage() for record in caplog.records]
    assert any(f"lab={lab.id}" in message and "timed_out=True" in message for message in messages)


async def test_failure_in_one_lab_does_not_touch_another(
    api: Api, root: Path, docker_client: aiodocker.Docker
) -> None:
    ana_lab = await api.lab(await api.account("ana@example.com"))
    before = await output(api, ana_lab, "cat ~/sample.sh")

    await with_setup(api, root, "echo bia > /tmp/who\nexit 1\n")
    await failed_lab(api, docker_client, "bia@example.com")

    assert await api.status(ana_lab) == ("ready", None)
    assert await output(api, ana_lab, "cat ~/sample.sh") == before
    assert (await run_in(api, ana_lab, "test -e /tmp/who")).exit_code != 0
