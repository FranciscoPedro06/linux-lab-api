import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import aiodocker

from linuxlab.labs.runtime import (
    ContainerInfo,
    ExecResult,
    ExecUser,
    LabContainerSpec,
    TerminalSession,
)
from linuxlab.labs.runtime.docker import DockerRuntime

OCI_RUNTIME = os.environ.get("LINUXLAB_TEST_OCI_RUNTIME", "runc")
LAB_IMAGE = os.environ.get("LINUXLAB_LAB_IMAGE", "linuxlab/lab-base:dev")
# Deployment label of containers created directly through the runtime by tests. No API
# instance, in tests or in a local environment, reconciles this deployment.
TEST_DEPLOYMENT = "runtime-tests"


def lab_spec(lab_id: str | None = None) -> LabContainerSpec:
    return LabContainerSpec(
        lab_id=lab_id or uuid.uuid4().hex, image=LAB_IMAGE, deployment=TEST_DEPLOYMENT
    )


class Container(Protocol):
    @property
    def id(self) -> str: ...


@asynccontextmanager
async def running_lab(runtime: DockerRuntime) -> AsyncIterator[ContainerInfo]:
    info = await runtime.create(lab_spec())
    try:
        await runtime.start(info.id)
        yield info
    finally:
        await runtime.remove(info.id)


async def sh(
    runtime: DockerRuntime,
    lab: Container,
    script: str,
    *,
    user: ExecUser = "student",
    time_limit: float = 20,
) -> ExecResult:
    return await runtime.exec(lab.id, ["sh", "-c", script], user=user, time_limit=time_limit)


async def docker_inspect(client: aiodocker.Docker, lab: ContainerInfo) -> dict[str, Any]:
    return await client.containers.container(lab.id).show()


async def read_until(terminal: TerminalSession, needle: bytes, seconds: float = 10) -> bytes:
    """Read terminal output until needle appears; fail after `seconds`."""
    output = b""
    async with asyncio.timeout(seconds):
        while needle not in output:
            chunk = await terminal.read()
            if chunk is None:
                raise AssertionError(f"terminal exited before {needle!r}: {output!r}")
            output += chunk
    return output


async def processes(runtime: DockerRuntime, lab: Container, pattern: str) -> list[str]:
    result = await sh(runtime, lab, f"pgrep -a -f '{pattern}' || true")
    return [line for line in result.stdout.decode().splitlines() if "pgrep" not in line]
