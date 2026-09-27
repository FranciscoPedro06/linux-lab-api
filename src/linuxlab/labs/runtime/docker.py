import asyncio
import time
from collections.abc import Sequence
from typing import Any

import aiodocker
import aiohttp
from aiodocker.exceptions import DockerError
from aiodocker.execs import Exec

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
from linuxlab.labs.runtime.spec import (
    LABEL_PREFIX,
    STUDENT_GID,
    STUDENT_HOME,
    STUDENT_UID,
    build_container_config,
    container_name,
)

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
            labels={
                key: value
                for key, value in (data["Config"].get("Labels") or {}).items()
                if key.startswith(LABEL_PREFIX)
            },
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
