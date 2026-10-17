"""The probe protocol on the API side: requests, answers and failures, on a FakeRuntime."""

import base64
import json
from collections.abc import Sequence
from typing import Any

import pytest

from linuxlab.labs.runtime import (
    ExecResult,
    ExecUser,
    LabContainerSpec,
    LabRuntimeError,
    RuntimeUnavailableError,
)
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.validation.probe import (
    MAX_FACTS,
    MAX_OUTPUT_BYTES,
    PROBE_COMMAND,
    PROBE_SECONDS,
    Fact,
    ProbeError,
    ProbeErrorCode,
    ProcessesFact,
    ReadFact,
    ReadResult,
    StatFact,
    StatResult,
    parse_answer,
    request_body,
    run_probe,
)

from .fake_probe import Entry, LabState, install

FACTS: list[Fact] = [StatFact("/home/student/a", follow=False), ReadFact("/home/student/a")]


def answer(*facts: dict[str, Any], v: int = 1) -> bytes:
    return json.dumps({"v": v, "facts": list(facts)}).encode()


STAT_OK = {
    "op": "stat",
    "path": "/home/student/a",
    "follow": False,
    "status": "ok",
    "type": "file",
    "mode": 0o600,
    "uid": 1000,
    "gid": 1000,
    "user": "student",
    "group": "student",
}
READ_OK = {
    "op": "read",
    "path": "/home/student/a",
    "status": "ok",
    "content": base64.b64encode(b"hello").decode(),
}


def test_request_has_a_version_and_only_the_facts() -> None:
    body = json.loads(request_body([*FACTS, ProcessesFact()]))
    assert body == {
        "v": 1,
        "facts": [
            {"op": "stat", "path": "/home/student/a", "follow": False},
            {"op": "read", "path": "/home/student/a"},
            {"op": "processes"},
        ],
    }


def test_request_limits() -> None:
    with pytest.raises(ValueError):
        request_body([])
    with pytest.raises(ValueError):
        request_body([ReadFact("/a"), ReadFact("/a")])
    request_body([ReadFact(f"/f{i}") for i in range(MAX_FACTS)])
    with pytest.raises(ValueError):
        request_body([ReadFact(f"/f{i}") for i in range(MAX_FACTS + 1)])
    with pytest.raises(ValueError, match="too large"):
        request_body([ReadFact("/" + "a" * 4000 + str(i)) for i in range(20)])


def test_a_valid_answer_is_parsed() -> None:
    facts = parse_answer(FACTS, answer(STAT_OK, READ_OK))
    stat = facts[FACTS[0]]
    read = facts[FACTS[1]]
    assert isinstance(stat, StatResult) and stat.mode == 0o600 and stat.user == "student"
    assert isinstance(read, ReadResult) and read.data == b"hello"


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"not json",
        b"[]",
        answer(STAT_OK, READ_OK, v=2),
        answer(READ_OK, STAT_OK),  # wrong order
        answer(STAT_OK),  # missing a fact
        answer(STAT_OK, READ_OK, READ_OK),  # one too many
        answer(STAT_OK, {**READ_OK, "path": "/home/student/b"}),  # another path
        answer({**STAT_OK, "follow": True}, READ_OK),
        answer({**STAT_OK, "status": "ok", "type": None}, READ_OK),
        answer({**STAT_OK, "mode": 0o10000}, READ_OK),
        answer({**STAT_OK, "extra": 1}, READ_OK),
        answer(STAT_OK, {**READ_OK, "content": "!!!"}),
        answer(STAT_OK, {**READ_OK, "status": "not_found"}),  # content with another status
        answer(STAT_OK, {**READ_OK, "content": base64.b64encode(b"x" * 65537).decode()}),
        answer(STAT_OK, {"op": "read", "path": "/home/student/a", "status": "error"}),
        answer(STAT_OK, {"op": "write", "path": "/home/student/a", "status": "ok"}),
        json.dumps({"v": 1, "facts": [STAT_OK, READ_OK], "more": 1}).encode(),
    ],
    ids=lambda output: f"{len(output)}-bytes",
)
def test_anomalous_answers_are_refused(output: bytes) -> None:
    with pytest.raises(ValueError):
        parse_answer(FACTS, output)


def test_statuses_without_details_are_accepted() -> None:
    facts = parse_answer(
        FACTS,
        answer(
            {"op": "stat", "path": "/home/student/a", "follow": False, "status": "not_found"},
            {"op": "read", "path": "/home/student/a", "status": "error", "errno": "EIO"},
        ),
    )
    assert facts[FACTS[0]].status == "not_found"
    assert facts[FACTS[1]].status == "error"


async def started(runtime: FakeRuntime) -> str:
    info = await runtime.create(LabContainerSpec(lab_id="a" * 32, image="lab", deployment="test"))
    await runtime.start(info.id)
    return info.id


async def test_run_probe_uses_a_fixed_command_as_root() -> None:
    runtime = FakeRuntime()
    container = await started(runtime)
    requests = install(runtime, LabState(files={"/home/student/a": Entry(content=b"hello")}))

    facts = await run_probe(runtime, container, FACTS)

    call = runtime.exec_calls[-1]
    assert call.argv == PROBE_COMMAND
    assert call.user == "root"
    assert call.time_limit == PROBE_SECONDS
    assert call.env == {}
    assert requests == [json.loads(request_body(FACTS))]
    read = facts[FACTS[1]]
    assert isinstance(read, ReadResult) and read.data == b"hello"


def result(**fields: Any) -> ExecResult:
    values: dict[str, Any] = {"exit_code": 0, "stdout": answer(STAT_OK, READ_OK), "stderr": b""}
    values.update(fields)
    return ExecResult(**values)


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (result(timed_out=True, exit_code=124), ProbeErrorCode.TIMEOUT),
        (result(exit_code=1, stdout=b""), ProbeErrorCode.FAILED),
        (result(truncated=True), ProbeErrorCode.INVALID_OUTPUT),
        (result(stdout=b" " * (MAX_OUTPUT_BYTES + 1)), ProbeErrorCode.INVALID_OUTPUT),
        (result(stdout=b"{"), ProbeErrorCode.INVALID_OUTPUT),
        (result(stdout=answer(STAT_OK, READ_OK, v=2)), ProbeErrorCode.INVALID_OUTPUT),
    ],
    ids=["timeout", "exit", "truncated", "oversized", "json", "version"],
)
async def test_probe_failures(outcome: ExecResult, code: ProbeErrorCode) -> None:
    runtime = FakeRuntime()
    container = await started(runtime)
    runtime.exec_handler = lambda argv, user: outcome

    with pytest.raises(ProbeError) as error:
        await run_probe(runtime, container, FACTS)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (RuntimeUnavailableError("docker down"), ProbeErrorCode.RUNTIME_UNAVAILABLE),
        (LabRuntimeError("exec failed"), ProbeErrorCode.FAILED),
    ],
)
async def test_runtime_errors(raised: Exception, code: ProbeErrorCode) -> None:
    runtime = FakeRuntime()
    container = await started(runtime)

    def handler(argv: Sequence[str], user: ExecUser) -> ExecResult:
        raise raised

    runtime.exec_handler = handler
    with pytest.raises(ProbeError) as error:
        await run_probe(runtime, container, FACTS)
    assert error.value.code == code


async def test_a_stopped_or_removed_container_is_reported_as_gone() -> None:
    runtime = FakeRuntime()
    container = await started(runtime)
    await runtime.stop(container)
    with pytest.raises(ProbeError) as error:
        await run_probe(runtime, container, FACTS)
    assert error.value.code == ProbeErrorCode.CONTAINER_GONE

    await runtime.remove(container)
    with pytest.raises(ProbeError) as error:
        await run_probe(runtime, container, FACTS)
    assert error.value.code == ProbeErrorCode.CONTAINER_GONE
