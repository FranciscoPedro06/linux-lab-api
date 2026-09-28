"""In-memory LabRuntime for unit tests of code that depends on the runtime.

It tracks container lifecycle and records exec calls. It does not run commands;
exec results come from the handler passed by the test. Terminals echo their input
back as output, like a PTY with echo on and nothing reading from it, and let the
test inject output, an exit or a failure.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from linuxlab.labs.runtime.base import (
    ContainerInfo,
    ContainerNotFoundError,
    ContainerNotRunningError,
    ExecResult,
    ExecUser,
    LabContainerSpec,
    LabRuntimeError,
    RuntimeUnavailableError,
    TerminalSize,
)
from linuxlab.labs.runtime.spec import LAB_ID_LABEL, MANAGED_LABEL, container_name

ExecHandler = Callable[[Sequence[str], ExecUser], ExecResult]


@dataclass(frozen=True)
class ExecCall:
    container_id: str
    argv: tuple[str, ...]
    user: ExecUser
    time_limit: float


@dataclass
class _Container:
    id: str
    name: str
    labels: dict[str, str]
    running: bool = False


# bytes: output; int: the shell exited with that code; error: the runtime failed;
# None: the session was closed.
_Event = bytes | int | LabRuntimeError | None


@dataclass
class FakeTerminalSession:
    container_id: str
    size: TerminalSize
    received: bytearray = field(default_factory=bytearray)
    resizes: list[TerminalSize] = field(default_factory=list)
    closed: bool = False
    _events: asyncio.Queue[_Event] = field(default_factory=asyncio.Queue)
    _exit_code: int | None = None
    _finished: bool = False

    def emit(self, data: bytes) -> None:
        self._events.put_nowait(data)

    def exit(self, code: int) -> None:
        self._events.put_nowait(code)

    def fail(self, error: LabRuntimeError) -> None:
        self._events.put_nowait(error)

    async def read(self) -> bytes | None:
        if self._finished:
            return None
        event = await self._events.get()
        if isinstance(event, LabRuntimeError):
            raise event
        if event is None or isinstance(event, int):
            self._finished = True
            if isinstance(event, int):
                self._exit_code = event
            return None
        return event

    async def write(self, data: bytes) -> None:
        if self.closed:
            raise LabRuntimeError("terminal is closed")
        self.received += data
        self.emit(data)

    async def resize(self, size: TerminalSize) -> None:
        self.resizes.append(size)

    async def close(self) -> int | None:
        if not self.closed:
            self.closed = True
            self._events.put_nowait(None)
        return self._exit_code


@dataclass
class FakeRuntime:
    available: bool = True
    exec_handler: ExecHandler | None = None
    exec_calls: list[ExecCall] = field(default_factory=list)
    terminals: list[FakeTerminalSession] = field(default_factory=list)
    _containers: dict[str, _Container] = field(default_factory=dict)
    _next_id: int = 0

    async def ping(self) -> None:
        if not self.available:
            raise RuntimeUnavailableError("fake runtime marked unavailable")

    async def create(self, spec: LabContainerSpec) -> ContainerInfo:
        name = container_name(spec.lab_id)
        if any(container.name == name for container in self._containers.values()):
            raise LabRuntimeError(f"Could not create container: name {name} is in use")
        self._next_id += 1
        container = _Container(
            id=f"fake-{self._next_id}",
            name=name,
            labels={MANAGED_LABEL: "true", LAB_ID_LABEL: spec.lab_id},
        )
        self._containers[container.id] = container
        return _info(container)

    async def start(self, container_id: str) -> None:
        self._get(container_id).running = True

    async def inspect(self, container_id: str) -> ContainerInfo:
        return _info(self._get(container_id))

    async def exec(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        user: ExecUser,
        time_limit: float,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must not be empty")
        if time_limit <= 0:
            raise ValueError("time_limit must be positive")
        if not self._get(container_id).running:
            raise ContainerNotRunningError(container_id)
        self.exec_calls.append(ExecCall(container_id, tuple(argv), user, time_limit))
        if self.exec_handler is None:
            return ExecResult(exit_code=0, stdout=b"", stderr=b"")
        return self.exec_handler(argv, user)

    async def open_terminal(self, container_id: str, size: TerminalSize) -> FakeTerminalSession:
        container = self._get(container_id)
        if not container.running:
            raise ContainerNotRunningError(container_id)
        terminal = FakeTerminalSession(container.id, size)
        self.terminals.append(terminal)
        return terminal

    async def stop(self, container_id: str) -> None:
        self._get(container_id).running = False

    async def remove(self, container_id: str) -> None:
        container = self._find(container_id)
        if container is not None:
            del self._containers[container.id]

    def _find(self, container_id: str) -> _Container | None:
        # Like Docker, accept either the id or the name.
        if container_id in self._containers:
            return self._containers[container_id]
        return next((c for c in self._containers.values() if c.name == container_id), None)

    def _get(self, container_id: str) -> _Container:
        container = self._find(container_id)
        if container is None:
            raise ContainerNotFoundError(container_id)
        return container


def _info(container: _Container) -> ContainerInfo:
    return ContainerInfo(
        id=container.id,
        name=container.name,
        running=container.running,
        labels=dict(container.labels),
    )
