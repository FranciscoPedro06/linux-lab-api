from collections.abc import Sequence

import pytest

from linuxlab.labs.runtime import (
    ContainerNotFoundError,
    ContainerNotRunningError,
    ExecResult,
    ExecUser,
    LabContainerSpec,
    LabRuntime,
    LabRuntimeError,
    RuntimeUnavailableError,
)
from linuxlab.labs.runtime.fake import ExecCall, FakeRuntime

SPEC = LabContainerSpec(lab_id="abc123", image="linuxlab/lab-base:test")


def test_fake_runtime_satisfies_protocol() -> None:
    runtime: LabRuntime = FakeRuntime()
    assert runtime is not None


async def test_create_returns_stopped_container_with_labels() -> None:
    runtime = FakeRuntime()

    info = await runtime.create(SPEC)

    assert info.name == "ll-lab-abc123"
    assert not info.running
    assert info.labels == {"linuxlab.managed": "true", "linuxlab.lab_id": "abc123"}


async def test_create_rejects_duplicate_name() -> None:
    runtime = FakeRuntime()
    await runtime.create(SPEC)

    with pytest.raises(LabRuntimeError):
        await runtime.create(SPEC)


async def test_lifecycle() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)

    await runtime.start(info.id)
    assert (await runtime.inspect(info.id)).running

    await runtime.stop(info.id)
    assert not (await runtime.inspect(info.id)).running

    await runtime.remove(info.id)
    with pytest.raises(ContainerNotFoundError):
        await runtime.inspect(info.id)


async def test_remove_missing_container_is_a_no_op() -> None:
    await FakeRuntime().remove("missing")


async def test_operations_on_missing_container_raise() -> None:
    runtime = FakeRuntime()

    with pytest.raises(ContainerNotFoundError):
        await runtime.start("missing")
    with pytest.raises(ContainerNotFoundError):
        await runtime.exec("missing", ["true"], user="student", time_limit=1)


async def test_exec_requires_running_container() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)

    with pytest.raises(ContainerNotRunningError):
        await runtime.exec(info.id, ["true"], user="student", time_limit=1)


async def test_exec_records_calls_and_uses_handler() -> None:
    def handler(argv: Sequence[str], user: ExecUser) -> ExecResult:
        return ExecResult(exit_code=3, stdout=user.encode(), stderr=b"")

    runtime = FakeRuntime(exec_handler=handler)
    info = await runtime.create(SPEC)
    await runtime.start(info.id)

    result = await runtime.exec(info.id, ["id", "-u"], user="root", time_limit=5)

    assert result == ExecResult(exit_code=3, stdout=b"root", stderr=b"")
    assert runtime.exec_calls == [ExecCall(info.id, ("id", "-u"), "root", 5)]


@pytest.mark.parametrize(
    ("argv", "time_limit"),
    [([], 1.0), (["true"], 0.0), (["true"], -1.0)],
)
async def test_exec_rejects_invalid_arguments(argv: list[str], time_limit: float) -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)
    await runtime.start(info.id)

    with pytest.raises(ValueError):
        await runtime.exec(info.id, argv, user="student", time_limit=time_limit)


async def test_ping_reports_unavailable_runtime() -> None:
    with pytest.raises(RuntimeUnavailableError):
        await FakeRuntime(available=False).ping()
