from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

# Only two identities ever run inside a lab: the student and the platform (root).
ExecUser = Literal["student", "root"]


@dataclass(frozen=True)
class LabContainerSpec:
    lab_id: str
    image: str


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    running: bool
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    truncated: bool = False


class LabRuntimeError(Exception):
    pass


class RuntimeUnavailableError(LabRuntimeError):
    pass


class ContainerNotFoundError(LabRuntimeError):
    pass


class ContainerNotRunningError(LabRuntimeError):
    pass


class LabRuntime(Protocol):
    async def ping(self) -> None:
        """Raise RuntimeUnavailableError if containers cannot be created."""

    async def create(self, spec: LabContainerSpec) -> ContainerInfo: ...

    async def start(self, container_id: str) -> None: ...

    async def inspect(self, container_id: str) -> ContainerInfo: ...

    async def exec(
        self,
        container_id: str,
        argv: Sequence[str],
        *,
        user: ExecUser,
        time_limit: float,
    ) -> ExecResult:
        """Run argv without a shell.

        time_limit is enforced inside the container; the call returns shortly after it.
        """

    async def stop(self, container_id: str) -> None: ...

    async def remove(self, container_id: str) -> None:
        """Remove the container, killing it if needed. Removing a missing container is a no-op."""
