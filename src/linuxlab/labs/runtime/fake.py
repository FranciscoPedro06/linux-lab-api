"""In-memory LabRuntime for unit tests of code that depends on the runtime.

It tracks container lifecycle and records exec calls. It does not run commands;
exec results come from the handler passed by the test.
"""

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


@dataclass
class FakeRuntime:
    available: bool = True
    exec_handler: ExecHandler | None = None
    exec_calls: list[ExecCall] = field(default_factory=list)
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

    async def stop(self, container_id: str) -> None:
        self._get(container_id).running = False

    async def remove(self, container_id: str) -> None:
        self._containers.pop(container_id, None)

    def _get(self, container_id: str) -> _Container:
        try:
            return self._containers[container_id]
        except KeyError:
            raise ContainerNotFoundError(container_id) from None


def _info(container: _Container) -> ContainerInfo:
    return ContainerInfo(
        id=container.id,
        name=container.name,
        running=container.running,
        labels=dict(container.labels),
    )
