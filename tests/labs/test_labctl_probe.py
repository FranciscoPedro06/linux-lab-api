"""labctl probe in real containers: the facts it reports, the paths it refuses to
follow, the requests it refuses, and what it leaves behind (nothing).

Runs under the runtime in LINUXLAB_TEST_OCI_RUNTIME (runc locally, runsc in the
Runtime workflow).
"""

import base64
import json
import time
from typing import Any

import pytest

from linuxlab.labs.runtime import ContainerInfo, ExecResult
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.validation import probe as probe_module
from linuxlab.validation.probe import (
    PROBE_COMMAND,
    ProbeError,
    ProbeErrorCode,
    ReadFact,
    StatFact,
    run_probe,
)

from .support import sh

pytestmark = pytest.mark.docker

HOME = "/home/student"


async def probe(runtime: DockerRuntime, lab: ContainerInfo, request: Any) -> ExecResult:
    stdin = request if isinstance(request, bytes) else json.dumps(request).encode()
    return await runtime.exec(lab.id, PROBE_COMMAND, user="root", time_limit=10, stdin=stdin)


async def facts(runtime: DockerRuntime, lab: ContainerInfo, *items: dict[str, Any]) -> list[Any]:
    result = await probe(runtime, lab, {"v": 1, "facts": list(items)})
    assert result.exit_code == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["v"] == 1
    found: list[Any] = answer["facts"]
    assert [{key: fact[key] for key in item} for fact, item in zip(found, items, strict=True)] == [
        *items
    ]
    return found


def stat(path: str, follow: bool = False) -> dict[str, Any]:
    return {"op": "stat", "path": path, "follow": follow}


def read(path: str) -> dict[str, Any]:
    return {"op": "read", "path": path}


async def test_stat_and_read_report_files(runtime: DockerRuntime, fresh_lab: ContainerInfo) -> None:
    await sh(runtime, fresh_lab, f"printf 'hello' > {HOME}/a && chmod 640 {HOME}/a")
    await sh(runtime, fresh_lab, f"mkdir {HOME}/d && ln -s {HOME}/d {HOME}/link")
    await sh(runtime, fresh_lab, "touch /tmp/root-file && chmod 600 /tmp/root-file", user="root")

    file, directory, link, followed, missing, owned_by_root, content = await facts(
        runtime,
        fresh_lab,
        stat(f"{HOME}/a"),
        stat(f"{HOME}/d"),
        stat(f"{HOME}/link"),
        stat(f"{HOME}/link", follow=True),
        stat(f"{HOME}/missing"),
        stat("/tmp/root-file"),
        read(f"{HOME}/a"),
    )

    assert file["status"] == "ok"
    assert (file["type"], file["mode"], file["uid"], file["user"], file["group"]) == (
        "file",
        0o640,
        1000,
        "student",
        "student",
    )
    assert directory["type"] == "directory"
    assert link["type"] == "symlink"
    assert followed["type"] == "directory"
    assert missing == {**stat(f"{HOME}/missing"), "status": "not_found"}
    assert (owned_by_root["user"], owned_by_root["group"], owned_by_root["mode"]) == (
        "root",
        "root",
        0o600,
    )
    assert base64.b64decode(content["content"]) == b"hello"


async def test_processes_are_listed_without_the_probe(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    await sh(runtime, fresh_lab, "setsid sleep 299 1 >/dev/null 2>&1 < /dev/null &")
    await sh(runtime, fresh_lab, "setsid sleep 301 >/dev/null 2>&1 < /dev/null &", user="root")

    (result,) = await facts(runtime, fresh_lab, {"op": "processes"})

    assert result["status"] == "ok"
    listed = [
        (entry["comm"], entry["user"], base64.b64decode(entry["args"]))
        for entry in result["processes"]
    ]
    assert ("sleep", "student", b"sleep\x00299\x001\x00") in listed
    assert ("sleep", "root", b"sleep\x00301\x00") in listed
    assert not any(b"labctl" in args for _, _, args in listed)


async def test_long_command_lines_are_cut(runtime: DockerRuntime, fresh_lab: ContainerInfo) -> None:
    await sh(
        runtime,
        fresh_lab,
        f"setsid sh -c 'sleep 300; true' pad {'x' * 6000} >/dev/null 2>&1 < /dev/null &",
    )
    (result,) = await facts(runtime, fresh_lab, {"op": "processes"})
    lengths = [len(base64.b64decode(entry["args"])) for entry in result["processes"]]
    assert max(lengths) == 4096


async def test_symbolic_links_are_never_read_through(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    # A root-only file holding a secret, as a root setup could leave it.
    await sh(
        runtime, fresh_lab, "echo TOKEN=s3cr3t > /tmp/secret && chmod 600 /tmp/secret", user="root"
    )
    await sh(runtime, fresh_lab, f"ln -s /tmp/secret {HOME}/deploy.sh")
    await sh(runtime, fresh_lab, f"ln -s /tmp {HOME}/tmp && ln -s /proc/1 {HOME}/init")

    final, middle, environ, through_stat = await facts(
        runtime,
        fresh_lab,
        read(f"{HOME}/deploy.sh"),
        read(f"{HOME}/tmp/secret"),
        read(f"{HOME}/init/environ"),
        stat(f"{HOME}/tmp/secret"),
    )

    assert final == {**read(f"{HOME}/deploy.sh"), "status": "wrong_type"}
    assert middle == {**read(f"{HOME}/tmp/secret"), "status": "symlink_in_path"}
    assert environ == {**read(f"{HOME}/init/environ"), "status": "symlink_in_path"}
    assert through_stat["status"] == "symlink_in_path"


async def test_a_fifo_or_device_is_not_opened_for_reading(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    await sh(runtime, fresh_lab, f"mkfifo {HOME}/fifo")
    started = time.monotonic()

    fifo, device, directory = await facts(
        runtime, fresh_lab, read(f"{HOME}/fifo"), read("/dev/zero"), read(HOME)
    )

    assert time.monotonic() - started < 8
    assert [fifo["status"], device["status"], directory["status"]] == ["wrong_type"] * 3


async def test_large_files_are_not_returned(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    await sh(runtime, fresh_lab, f"head -c 65537 /dev/zero > {HOME}/big")
    await sh(runtime, fresh_lab, f"head -c 65536 /dev/zero > {HOME}/limit")
    big, limit = await facts(runtime, fresh_lab, read(f"{HOME}/big"), read(f"{HOME}/limit"))
    assert big == {**read(f"{HOME}/big"), "status": "too_large"}
    assert len(base64.b64decode(limit["content"])) == 65536


async def test_paths_never_reach_a_shell(runtime: DockerRuntime, fresh_lab: ContainerInfo) -> None:
    paths = [
        f"{HOME}/$(touch /tmp/pwned)",
        f"{HOME}/`touch /tmp/pwned`",
        f"{HOME}/a; touch /tmp/pwned",
        f"{HOME}/a && touch /tmp/pwned",
        f"{HOME}/*",
    ]
    found = await facts(runtime, fresh_lab, *[read(path) for path in paths])
    assert {fact["status"] for fact in found} == {"not_found"}
    assert (await sh(runtime, fresh_lab, "test -e /tmp/pwned")).exit_code != 0


@pytest.mark.parametrize(
    "request_",
    [
        b"",
        b"not json",
        b"[]",
        {"v": 2, "facts": [{"op": "processes"}]},
        {"v": True, "facts": [{"op": "processes"}]},
        {"v": 1, "facts": []},
        {"v": 1, "facts": [{"op": "write", "path": "/tmp/x"}]},
        {"v": 1, "facts": [{"op": "exec", "argv": ["id"]}]},
        {"v": 1, "facts": [{"op": "read", "path": "relative"}]},
        {"v": 1, "facts": [{"op": "read", "path": "/home/student/../../etc/shadow"}]},
        {"v": 1, "facts": [{"op": "read", "path": "/home//student"}]},
        {"v": 1, "facts": [{"op": "read", "path": "/home/student/"}]},
        {"v": 1, "facts": [{"op": "read", "path": "/home/\u0000x"}]},
        {"v": 1, "facts": [{"op": "read", "path": "/" + "a" * 4096}]},
        {"v": 1, "facts": [{"op": "read", "path": "/x", "follow": True}]},
        {"v": 1, "facts": [{"op": "stat", "path": "/x", "follow": "yes"}]},
        {"v": 1, "facts": [{"op": "processes"}, {"op": "processes"}]},
        {"v": 1, "facts": [{"op": "read", "path": f"/f{i}"} for i in range(52)]},
        {"v": 1, "facts": [{"op": "processes"}], "extra": 1},
        b'{"v": 1, "facts": [{"op": "processes"}]}' + b" " * (64 * 1024),
    ],
    ids=lambda value: str(len(json.dumps(value) if not isinstance(value, bytes) else value)),
)
async def test_invalid_requests_are_refused(
    runtime: DockerRuntime, lab: ContainerInfo, request_: Any
) -> None:
    result = await probe(runtime, lab, request_)
    assert result.exit_code == 1
    assert result.stdout == b""
    assert result.stderr.startswith(b"labctl: probe:")


async def test_the_probe_writes_nothing_and_leaves_no_process(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    listing = "find /tmp /home/student /run/lab -printf '%p %s %m %T@\n' | sort"
    await sh(runtime, fresh_lab, f"printf x > {HOME}/a")
    before = (await sh(runtime, fresh_lab, listing, user="root")).stdout

    for _ in range(3):
        await facts(runtime, fresh_lab, stat(f"{HOME}/a"), read(f"{HOME}/a"), {"op": "processes"})

    assert (await sh(runtime, fresh_lab, listing, user="root")).stdout == before
    remaining = (await sh(runtime, fresh_lab, "ps -eo user=,args=")).stdout.decode()
    assert "labctl" not in remaining
    assert "python3" not in remaining


async def test_the_student_cannot_run_the_probe(runtime: DockerRuntime, lab: ContainerInfo) -> None:
    result = await runtime.exec(
        lab.id, PROBE_COMMAND, user="student", time_limit=10, stdin=b'{"v":1,"facts":[]}'
    )
    assert result.exit_code != 0
    assert result.stdout == b""


async def test_run_probe_against_a_real_lab(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    await sh(runtime, fresh_lab, f"printf 'TOKEN=1' > {HOME}/a && chmod 600 {HOME}/a")
    collected = await run_probe(
        runtime, fresh_lab.id, [StatFact(f"{HOME}/a", follow=False), ReadFact(f"{HOME}/a")]
    )
    assert [result.status for result in collected.values()] == ["ok", "ok"]


async def test_a_probe_that_does_not_finish_times_out(
    runtime: DockerRuntime, fresh_lab: ContainerInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stand-in command that never answers, to exercise the real time limit.
    monkeypatch.setattr(probe_module, "PROBE_COMMAND", ("sleep", "30"))
    monkeypatch.setattr(probe_module, "PROBE_SECONDS", 1)
    started = time.monotonic()
    with pytest.raises(ProbeError) as error:
        await run_probe(runtime, fresh_lab.id, [ReadFact(f"{HOME}/a")])
    assert error.value.code == ProbeErrorCode.TIMEOUT
    assert time.monotonic() - started < 15
    remaining = (await sh(runtime, fresh_lab, "ps -eo args=")).stdout.decode()
    assert "sleep 30" not in remaining


async def test_a_stopped_container_cannot_be_probed(
    runtime: DockerRuntime, fresh_lab: ContainerInfo
) -> None:
    await runtime.stop(fresh_lab.id)
    with pytest.raises(ProbeError) as error:
        await run_probe(runtime, fresh_lab.id, [ReadFact(f"{HOME}/a")])
    assert error.value.code == ProbeErrorCode.CONTAINER_GONE
