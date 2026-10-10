import asyncio
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
    TerminalSize,
)
from linuxlab.labs.runtime.fake import ExecCall, FakeRuntime

SPEC = LabContainerSpec(lab_id="abc123", image="linuxlab/lab-base:test", deployment="tests")


def test_fake_runtime_satisfies_protocol() -> None:
    runtime: LabRuntime = FakeRuntime()
    assert runtime is not None


async def test_create_returns_stopped_container_with_labels() -> None:
    runtime = FakeRuntime()

    info = await runtime.create(SPEC)

    assert info.name == "ll-lab-abc123"
    assert not info.running
    assert info.labels == {
        "linuxlab.managed": "true",
        "linuxlab.lab_id": "abc123",
        "linuxlab.deployment": "tests",
    }


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


async def test_exec_records_stdin_and_parameters() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)
    await runtime.start(info.id)

    await runtime.exec(
        info.id,
        ["bash"],
        user="student",
        time_limit=5,
        stdin=b"echo hi\n",
        env={"LAB_PARAM_TOKEN": "ab12"},
    )

    assert runtime.exec_calls == [
        ExecCall(info.id, ("bash",), "student", 5, b"echo hi\n", {"LAB_PARAM_TOKEN": "ab12"})
    ]


@pytest.mark.parametrize(
    "env",
    [
        {"PATH": "/tmp"},
        {"HOME": "/root"},
        {"LAB_PARAM_": "a"},
        {"LAB_PARAM_token": "a"},
        {"LAB_PARAM_" + "A" * 33: "a"},
        {"LAB_PARAM_TOKEN": ""},
        {"LAB_PARAM_TOKEN": "A"},
        {"LAB_PARAM_TOKEN": "a b"},
        {"LAB_PARAM_TOKEN": "a\n"},
        {"LAB_PARAM_TOKEN": "a" * 65},
    ],
)
async def test_exec_refuses_other_variables_without_quoting_values(env: dict[str, str]) -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)
    await runtime.start(info.id)

    with pytest.raises(ValueError) as error:
        await runtime.exec(info.id, ["true"], user="student", time_limit=5, env=env)
    for value in env.values():
        if value:
            assert repr(value) not in str(error.value)
    assert runtime.exec_calls == []


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


@pytest.mark.parametrize(("cols", "rows"), [(0, 24), (80, 0), (501, 24), (80, 201), (-1, -1)])
def test_terminal_size_rejects_out_of_range_values(cols: int, rows: int) -> None:
    with pytest.raises(ValueError):
        TerminalSize(cols, rows)


def test_terminal_size_accepts_bounds() -> None:
    assert TerminalSize(1, 1) == TerminalSize(cols=1, rows=1)
    assert TerminalSize(500, 200).cols == 500


async def test_containers_can_be_found_by_name() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)

    assert (await runtime.inspect(info.name)).id == info.id


async def test_fake_terminal_echoes_input_and_reports_exit() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)
    await runtime.start(info.id)

    terminal = await runtime.open_terminal(info.id, TerminalSize(80, 24))
    await terminal.write(b"ls\r")
    await terminal.resize(TerminalSize(100, 30))
    terminal.exit(0)

    assert await terminal.read() == b"ls\r"
    assert await terminal.read() is None
    assert await terminal.close() == 0
    assert terminal.resizes == [TerminalSize(100, 30)]


async def test_fake_terminal_close_unblocks_reader() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)
    await runtime.start(info.id)
    terminal = await runtime.open_terminal(info.id, TerminalSize(80, 24))

    await terminal.close()

    assert await terminal.read() is None


async def test_terminal_requires_running_container() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)

    with pytest.raises(ContainerNotRunningError):
        await runtime.open_terminal(info.id, TerminalSize(80, 24))


async def test_list_labs_selects_by_deployment_and_managed_label() -> None:
    runtime = FakeRuntime()
    mine = await runtime.create(SPEC)
    await runtime.create(LabContainerSpec(lab_id="def456", image="test", deployment="other"))
    runtime.add_container("ll-lab-0000", {"linuxlab.deployment": "tests"})

    assert [info.id for info in await runtime.list_labs("tests")] == [mine.id]


async def test_crash_stops_the_container_and_ends_its_terminals() -> None:
    runtime = FakeRuntime()
    info = await runtime.create(SPEC)
    await runtime.start(info.id)
    terminal = await runtime.open_terminal(info.id, TerminalSize(80, 24))

    runtime.crash(info.id, oom=True)

    inspected = await runtime.inspect(info.id)
    assert not inspected.running
    assert inspected.oom_killed
    assert await terminal.read() is None


async def test_failures_can_be_injected_per_operation() -> None:
    runtime = FakeRuntime(fail={"start"})
    info = await runtime.create(SPEC)

    with pytest.raises(LabRuntimeError):
        await runtime.start(info.id)
    runtime.available = False
    with pytest.raises(LabRuntimeError):
        await runtime.inspect(info.id)


async def test_gated_operation_waits_for_its_event() -> None:
    gate = asyncio.Event()
    runtime = FakeRuntime(gates={"create": gate})

    creating = asyncio.create_task(runtime.create(SPEC))
    await asyncio.sleep(0.05)
    assert not creating.done()
    gate.set()

    assert (await creating).name == "ll-lab-abc123"
