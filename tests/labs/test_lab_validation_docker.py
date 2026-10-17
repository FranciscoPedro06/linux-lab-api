"""Validation in real containers, through the API: the synthetic missions' solutions
pass, their counterexamples fail, labs do not see each other, and nothing the
student can do in the lab makes the probe read what it should not.

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc locally, runsc in the
Runtime workflow), with PostgreSQL.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiodocker
import httpx2
import pytest

from linuxlab.auth.cookies import SESSION_COOKIE
from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime.docker import DockerRuntime
from tests.auth.support import ORIGIN
from tests.content.support import FIXTURE, copy_fixture, edit_yaml, mission_file

from .app_support import MISSION, Account, Api, running_api
from .support import LAB_IMAGE, OCI_RUNTIME
from .test_lab_setup_docker import output, run_in

pytestmark = [pytest.mark.docker, pytest.mark.integration]

SOLUTIONS = FIXTURE / "missions" / MISSION / "tests"


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


async def post_validate(
    api: Api, account: Account, lab: LabSession, **body: Any
) -> httpx2.Response:
    async with httpx2.AsyncClient(base_url=api.url.replace("ws://", "http://")) as client:
        return await client.post(
            f"/api/labs/{lab.id}/validate",
            json=body,
            headers={"origin": ORIGIN, "cookie": f"{SESSION_COOKIE}={account.token}"},
        )


async def validate(api: Api, account: Account, lab: LabSession, **body: Any) -> dict[str, Any]:
    response = await post_validate(api, account, lab, **body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def statuses(body: dict[str, Any]) -> dict[str, str]:
    return {child["id"]: child["status"] for child in body["result"]["children"]}


async def solve(api: Api, lab: LabSession, script: str) -> None:
    await output(api, lab, (SOLUTIONS / script).read_text(encoding="utf-8"))


async def test_a_fresh_lab_fails_and_each_solution_passes(api: Api) -> None:
    for index, solution in enumerate(("solution-octal.sh", "solution-symbolic.sh")):
        account = await api.account(f"ana{index}@example.com")
        lab = await api.lab(account)

        fresh = await validate(api, account, lab)
        assert fresh["outcome"] == "failed"
        assert statuses(fresh) == {"exists": "passed", "content-kept": "passed", "mode": "failed"}

        await solve(api, lab, solution)
        assert (await validate(api, account, lab))["outcome"] == "passed", solution
        await api.end(lab)


async def test_the_counterexample_fails(api: Api) -> None:
    account = await api.account("ana@example.com")
    lab = await api.lab(account)
    await solve(api, lab, "counter-empty.sh")

    body = await validate(api, account, lab)

    assert body["outcome"] == "failed"
    assert statuses(body) == {"exists": "passed", "content-kept": "failed", "mode": "passed"}


async def test_the_way_the_state_is_reached_does_not_matter(api: Api) -> None:
    account = await api.account("ana@example.com")
    lab = await api.lab(account)
    # Recreate the file with the same content under a restrictive umask: no chmod.
    await output(
        api,
        lab,
        "cp ~/sample.sh /tmp/copy && rm ~/sample.sh && umask 077 && cat /tmp/copy > ~/sample.sh",
    )
    assert (await validate(api, account, lab))["outcome"] == "passed"


async def test_a_symbolic_link_to_a_root_only_copy_of_the_secret_does_not_pass(
    api: Api, tmp_path: Path
) -> None:
    # A root setup keeps a root-only copy of the parameter; the student cannot read it
    # but could point the checked path at it.
    root = copy_fixture(tmp_path)
    (root / "missions" / MISSION / "setup.sh").write_text(
        'printf "TOKEN=%s\\n" "$LAB_PARAM_TOKEN" > /tmp/secret\nchmod 600 /tmp/secret\n'
        'printf "nothing here\\n" > /home/student/sample.sh\n'
        "chown 1000:1000 /home/student/sample.sh\nchmod 644 /home/student/sample.sh\n",
        encoding="utf-8",
    )
    edit_yaml(mission_file(root, MISSION), lambda data: data["setup"].update(user="root"))
    await api.sync(root)
    account = await api.account("ana@example.com")
    lab = await api.lab(account)
    assert (await run_in(api, lab, "cat /tmp/secret")).exit_code != 0

    await output(api, lab, "rm ~/sample.sh && ln -s /tmp/secret ~/sample.sh")
    linked = await validate(api, account, lab)

    assert linked["outcome"] == "failed"
    assert statuses(linked)["content-kept"] == "failed"
    assert statuses(linked)["exists"] == "failed"
    rows = await api.sql("SELECT result FROM validation_runs ORDER BY id")
    assert [child["reason"] for child in rows[-1][0]["children"]] == ["wrong_type"] * 3


async def test_labs_are_validated_in_isolation(api: Api) -> None:
    ana, bia = await api.account("ana@example.com"), await api.account("bia@example.com")
    ana_lab, bia_lab = await api.lab(ana), await api.lab(bia)
    await solve(api, ana_lab, "solution-octal.sh")

    assert (await validate(api, ana, ana_lab))["outcome"] == "passed"
    assert (await validate(api, bia, bia_lab))["outcome"] == "failed"
    rows = await api.sql("SELECT lab_session_id, outcome FROM validation_runs ORDER BY id")
    assert [(row[0], row[1]) for row in rows] == [
        (ana_lab.id, "passed"),
        (bia_lab.id, "failed"),
    ]


async def test_validation_leaves_the_lab_as_it_was(api: Api) -> None:
    account = await api.account("ana@example.com")
    lab = await api.lab(account)
    listing = "find /tmp /home/student /run/lab -printf '%p %s %m\\n' | sort; ps -eo user=,args="
    before = await output(api, lab, listing, user="root")
    stored = await api.sql("SELECT params, mission_version, status FROM lab_sessions")

    await validate(api, account, lab)
    await validate(api, account, lab)

    after = await output(api, lab, listing, user="root")
    assert [
        line for line in after.splitlines() if "find " not in line and "ps -eo" not in line
    ] == [line for line in before.splitlines() if "find " not in line and "ps -eo" not in line]
    assert await api.sql("SELECT params, mission_version, status FROM lab_sessions") == stored


async def test_an_answer_mission_in_a_real_lab(api: Api) -> None:
    account = await api.account("ana@example.com")
    lab = await api.lab(account, "sample-answer")
    word = (await output(api, lab, "cat ~/.palavra")).strip()

    assert (await validate(api, account, lab, answer="wrong"))["outcome"] == "failed"
    assert (await validate(api, account, lab, answer=word))["outcome"] == "passed"


async def test_a_lab_whose_container_died_is_not_validated(api: Api) -> None:
    account = await api.account("ana@example.com")
    lab = await api.lab(account)
    runtime: DockerRuntime = api.app.state.runtime
    info = await api.labs.running_container(lab)
    await runtime.remove(info.id)

    response = await post_validate(api, account, lab)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "lab_not_ready"
    assert await api.status(lab) == ("terminated", "container_lost")
