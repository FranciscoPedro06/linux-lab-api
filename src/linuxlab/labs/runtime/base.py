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


# Bounds for a terminal size, checked before anything reaches Docker.
MAX_TERMINAL_COLS = 500
MAX_TERMINAL_ROWS = 200


@dataclass(frozen=True)
class TerminalSize:
    cols: int
    rows: int

    def __post_init__(self) -> None:
        if not (1 <= self.cols <= MAX_TERMINAL_COLS and 1 <= self.rows <= MAX_TERMINAL_ROWS):
            raise ValueError(f"terminal size out of range: {self.cols}x{self.rows}")


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


class TerminalSession(Protocol):
    """An interactive login shell running as the student on a PTY inside a lab."""

    async def read(self) -> bytes | None:
        """Return the next chunk of output as it arrives, or None once the shell has exited."""

    async def write(self, data: bytes) -> None: ...

    async def resize(self, size: TerminalSize) -> None: ...

    async def close(self) -> int | None:
        """End the shell and every process started from it.

        Returns the shell's exit code if it exited on its own. Safe to call more than once.
        """


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

    async def open_terminal(self, container_id: str, size: TerminalSize) -> TerminalSession:
        """Start an interactive login shell as the student. The user cannot be chosen."""

    async def stop(self, container_id: str) -> None: ...

    async def remove(self, container_id: str) -> None:
        """Remove the container, killing it if needed. Removing a missing container is a no-op."""
