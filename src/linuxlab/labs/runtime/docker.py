import asyncio
import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any

import aiodocker
import aiohttp
from aiodocker.exceptions import DockerError
from aiodocker.execs import Exec
from aiodocker.stream import Stream

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
from linuxlab.labs.runtime.spec import (
    DEPLOYMENT_LABEL,
    LABEL_PREFIX,
    MANAGED_LABEL,
    STUDENT_GID,
    STUDENT_HOME,
    STUDENT_UID,
    build_container_config,
    container_name,
)

logger = logging.getLogger(__name__)

EXEC_IDENTITIES: dict[ExecUser, tuple[str, str]] = {
    "student": (f"{STUDENT_UID}:{STUDENT_GID}", STUDENT_HOME),
    "root": ("0:0", "/"),
}
EXEC_ENV = [
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG=C.UTF-8",
]
MAX_OUTPUT_BYTES = 1024 * 1024

# argv runs under coreutils `timeout`, which signals the whole process group:
# SIGTERM at the deadline, SIGKILL KILL_AFTER_SECONDS later. The client gives up
# CLIENT_MARGIN_SECONDS after that in case the exec itself hangs.
KILL_AFTER_SECONDS = 2
CLIENT_MARGIN_SECONDS = 5
TIMEOUT_EXIT_CODE = 124
KILLED_EXIT_CODE = 137

STOP_GRACE_SECONDS = 2

TERMINAL_COMMAND = ["bash", "--login"]
TERMINAL_MARKER = "LINUXLAB_TERMINAL"
TERMINAL_CONTROL_SECONDS = 5
TERMINAL_CLEANUP_SECONDS = 10
# How long a failed cleanup waits for the container to stop before reporting an error.
SETTLE_SECONDS = 2

# Closing the connection to a TTY exec does not end anything inside the container:
# the shell and everything started from it keep running. On close, this runs as the
# student and ends the terminal the way a hangup would: SIGHUP, then SIGKILL, to every
# process in the shell's session and every process that still carries the terminal's
# marker (for example after setsid). It only reaches the student's own processes.
END_TERMINAL_SCRIPT = r"""
import os, signal, sys, time

marker = f"{sys.argv[1]}={sys.argv[2]}".encode()
# Never target this cleanup itself: its process, its parent (timeout) or its session.
# Session ids are pids, and a pid freed by the terminal's shell can be reused here.
own_pids = {os.getpid(), os.getppid()}
own_session = os.getsid(0)


def pids():
    found = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    return [pid for pid in found if pid not in own_pids]


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            return stat.read().rsplit(b")", 1)[1].split()[0] != b"Z"
    except (OSError, IndexError):
        return False


def marked(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as environ:
            return marker in environ.read().split(b"\0")
    except OSError:
        return False


def sid(pid):
    try:
        return os.getsid(pid)
    except OSError:
        return None


sessions = {sid(pid) for pid in pids() if marked(pid)} - {None, own_session}


def targets():
    return [
        pid
        for pid in pids()
        if alive(pid) and sid(pid) != own_session and (sid(pid) in sessions or marked(pid))
    ]


for signum in (signal.SIGHUP, signal.SIGKILL):
    for pid in targets():
        try:
            os.kill(pid, signum)
        except OSError:
            pass
    deadline = time.monotonic() + 1
    while targets() and time.monotonic() < deadline:
        time.sleep(0.05)
print(len(targets()))
"""


class DockerRuntime:
    def __init__(self, client: aiodocker.Docker, *, oci_runtime: str) -> None:
        self._client = client
        self._oci_runtime = oci_runtime

    async def ping(self) -> None:
        try:
            info = await self._client.system.info()
        except (DockerError, aiohttp.ClientError, OSError) as error:
            raise RuntimeUnavailableError(f"Docker is not reachable: {error}") from error
        if self._oci_runtime not in info.get("Runtimes", {}):
            raise RuntimeUnavailableError(
                f"OCI runtime {self._oci_runtime!r} is not registered with Docker"
            )

    async def create(self, spec: LabContainerSpec) -> ContainerInfo:
        config = build_container_config(spec, self._oci_runtime)
        try:
            container = await self._client.containers.create(
                config, name=container_name(spec.lab_id)
            )
        except DockerError as error:
            raise LabRuntimeError(f"Could not create container: {error.message}") from error
        return await self.inspect(container.id)

    async def list_labs(self, deployment: str) -> list[ContainerInfo]:
        labels = [f"{MANAGED_LABEL}=true", f"{DEPLOYMENT_LABEL}={deployment}"]
        try:
            containers = await self._client.containers.list(
                all="true", filters=json.dumps({"label": labels})
            )
        except (DockerError, aiohttp.ClientError, OSError) as error:
            raise LabRuntimeError(f"Could not list lab containers: {error}") from error
        return [
            ContainerInfo(
                id=container["Id"],
                name=(container["Names"] or [""])[0].lstrip("/"),
                running=container["State"] == "running",
                labels=_lab_labels(container["Labels"]),
            )
            for container in containers
        ]

    async def start(self, container_id: str) -> None:
        try:
            await self._client.containers.container(container_id).start()
        except DockerError as error:
            raise _translate(error, container_id) from error

    async def inspect(self, container_id: str) -> ContainerInfo:
        data = await self._show(container_id)
        return ContainerInfo(
            id=data["Id"],
            name=data["Name"].lstrip("/"),
            running=bool(data["State"]["Running"]),
            labels=_lab_labels(data["Config"].get("Labels")),
            oom_killed=bool(data["State"].get("OOMKilled")),
        )

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
        if not (await self.inspect(container_id)).running:
            raise ContainerNotRunningError(container_id)

        uid_gid, workdir = EXEC_IDENTITIES[user]
        command = ["timeout", f"--kill-after={KILL_AFTER_SECONDS}", f"{time_limit:g}", *argv]
        try:
            execution = await self._client.containers.container(container_id).exec(
                cmd=command,
                user=uid_gid,
                environment=EXEC_ENV,
                workdir=workdir,
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
            )
        except DockerError as error:
            raise _translate(error, container_id) from error

        started = time.monotonic()
        try:
            async with asyncio.timeout(time_limit + KILL_AFTER_SECONDS + CLIENT_MARGIN_SECONDS):
                stdout, stderr, truncated = await _collect_output(execution)
                exit_code = await _wait_exit_code(execution)
        except TimeoutError:
            return ExecResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True)

        elapsed = time.monotonic() - started
        timed_out = exit_code == TIMEOUT_EXIT_CODE or (
            exit_code == KILLED_EXIT_CODE and elapsed >= time_limit
        )
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=truncated,
        )

    async def open_terminal(self, container_id: str, size: TerminalSize) -> "DockerTerminalSession":
        if not (await self.inspect(container_id)).running:
            raise ContainerNotRunningError(container_id)

        token = uuid.uuid4().hex
        uid_gid, workdir = EXEC_IDENTITIES["student"]
        try:
            execution = await self._client.containers.container(container_id).exec(
                cmd=TERMINAL_COMMAND,
                user=uid_gid,
                environment=[*EXEC_ENV, "TERM=xterm-256color", f"{TERMINAL_MARKER}={token}"],
                workdir=workdir,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=True,
            )
            stream = execution.start(detach=False)
            async with asyncio.timeout(TERMINAL_CONTROL_SECONDS):
                await stream.__aenter__()
                await execution.resize(h=size.rows, w=size.cols)
        except (DockerError, aiohttp.ClientError, OSError, TimeoutError) as error:
            raise LabRuntimeError(f"{container_id}: could not start terminal: {error}") from error
        return DockerTerminalSession(self, container_id, execution, stream, token)

    async def end_terminal_processes(self, container_id: str, token: str) -> int:
        """Hang up a terminal's processes. Returns how many are still alive afterwards."""
        result = await self.exec(
            container_id,
            ["python3", "-I", "-c", END_TERMINAL_SCRIPT, TERMINAL_MARKER, token],
            user="student",
            time_limit=TERMINAL_CLEANUP_SECONDS,
        )
        if result.exit_code != 0:
            # The lab may have been stopped or removed while the cleanup ran; then
            # there is nothing left to end.
            if await self._stops_soon(container_id):
                return 0
            raise LabRuntimeError(
                f"{container_id}: terminal cleanup failed: exit {result.exit_code}, "
                f"timed out {result.timed_out}, stderr {result.stderr!r}"
            )
        return int(result.stdout)

    async def _stops_soon(self, container_id: str) -> bool:
        """Whether the container is gone or stops within SETTLE_SECONDS.

        An exec ends as soon as its container is killed, but Docker keeps reporting
        the container as running for some tens of milliseconds afterwards, under
        runc and runsc alike, so a single inspect right after the exec can still
        see it running.
        """
        try:
            async with asyncio.timeout(SETTLE_SECONDS):
                await self._client.containers.container(container_id).wait(condition="not-running")
        except TimeoutError:
            return False
        except DockerError as error:
            if error.status == 404:
                return True
            raise _translate(error, container_id) from error
        return True

    async def stop(self, container_id: str) -> None:
        try:
            await self._client.containers.container(container_id).stop(t=STOP_GRACE_SECONDS)
        except DockerError as error:
            raise _translate(error, container_id) from error

    async def remove(self, container_id: str) -> None:
        try:
            await self._client.containers.container(container_id).delete(force=True)
        except DockerError as error:
            if error.status != 404:
                raise _translate(error, container_id) from error

    async def _show(self, container_id: str) -> dict[str, Any]:
        try:
            return await self._client.containers.container(container_id).show()
        except DockerError as error:
            raise _translate(error, container_id) from error


class DockerTerminalSession:
    """A `bash --login` exec with a PTY (Tty=true, stdin attached) as the student.

    With a TTY, Docker sends raw output bytes rather than the multiplexed stdout/stderr
    format, and input is written to the PTY as-is: keystrokes, control characters and
    escape sequences reach the shell exactly as the browser sent them.
    """

    def __init__(
        self,
        runtime: DockerRuntime,
        container_id: str,
        execution: Exec,
        stream: Stream,
        token: str,
    ) -> None:
        self._runtime = runtime
        self._container_id = container_id
        self._execution = execution
        self._stream = stream
        self._token = token
        self._exited = False
        self._closed = False
        self._exit_code: int | None = None

    async def read(self) -> bytes | None:
        if self._exited:
            return None
        try:
            message = await self._stream.read_out()
        except (DockerError, aiohttp.ClientError, OSError) as error:
            raise LabRuntimeError(f"{self._container_id}: terminal read failed: {error}") from error
        if message is None:
            self._exited = True
            return None
        return message.data

    async def write(self, data: bytes) -> None:
        try:
            async with asyncio.timeout(TERMINAL_CONTROL_SECONDS):
                await self._stream.write_in(data)
        except (DockerError, aiohttp.ClientError, OSError, RuntimeError, TimeoutError) as error:
            raise LabRuntimeError(
                f"{self._container_id}: terminal write failed: {error}"
            ) from error

    async def resize(self, size: TerminalSize) -> None:
        try:
            async with asyncio.timeout(TERMINAL_CONTROL_SECONDS):
                await self._execution.resize(h=size.rows, w=size.cols)
        except (DockerError, aiohttp.ClientError, OSError, TimeoutError) as error:
            raise LabRuntimeError(
                f"{self._container_id}: terminal resize failed: {error}"
            ) from error

    async def close(self) -> int | None:
        if self._closed:
            return self._exit_code
        self._closed = True
        exited_on_its_own = self._exited
        # A failure to close the stream must not skip the cleanup below, or the
        # shell and its processes would outlive the terminal.
        try:
            await self._stream.close()
        except Exception:
            logger.warning(
                "terminal stream did not close cleanly: container=%s",
                self._container_id,
                exc_info=True,
            )
        try:
            remaining = await self._runtime.end_terminal_processes(self._container_id, self._token)
            if remaining:
                logger.warning(
                    "terminal processes still alive after cleanup: container=%s count=%d",
                    self._container_id,
                    remaining,
                )
        except (ContainerNotRunningError, ContainerNotFoundError):
            pass  # The lab stopped or was removed (for example after an OOM); nothing is left.
        if exited_on_its_own:
            self._exit_code = await self._exit_code_or_none()
        return self._exit_code

    async def _exit_code_or_none(self) -> int | None:
        try:
            async with asyncio.timeout(TERMINAL_CONTROL_SECONDS):
                return await _wait_exit_code(self._execution)
        except (DockerError, aiohttp.ClientError, OSError, TimeoutError):
            return None


def _lab_labels(labels: dict[str, str] | None) -> dict[str, str]:
    return {key: value for key, value in (labels or {}).items() if key.startswith(LABEL_PREFIX)}


def _translate(error: DockerError, container_id: str) -> LabRuntimeError:
    if error.status == 404:
        return ContainerNotFoundError(container_id)
    if error.status == 409:
        return ContainerNotRunningError(f"{container_id}: {error.message}")
    return LabRuntimeError(f"{container_id}: {error.message}")


async def _collect_output(execution: Exec) -> tuple[bytes, bytes, bool]:
    stdout = bytearray()
    stderr = bytearray()
    truncated = False
    async with execution.start(detach=False) as stream:
        while (message := await stream.read_out()) is not None:
            target = stdout if message.stream == 1 else stderr
            room = MAX_OUTPUT_BYTES - len(target)
            if len(message.data) > room:
                truncated = True
            target += message.data[: max(room, 0)]
    return bytes(stdout), bytes(stderr), truncated


async def _wait_exit_code(execution: Exec) -> int:
    # The output stream can close slightly before Docker records the exit code.
    while True:
        details = await execution.inspect()
        if not details["Running"] and details["ExitCode"] is not None:
            return int(details["ExitCode"])
        await asyncio.sleep(0.05)
