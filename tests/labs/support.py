import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiodocker

from linuxlab.labs.runtime import ContainerInfo, ExecResult, ExecUser, LabContainerSpec
from linuxlab.labs.runtime.docker import DockerRuntime

OCI_RUNTIME = os.environ.get("LINUXLAB_TEST_OCI_RUNTIME", "runc")
LAB_IMAGE = os.environ.get("LINUXLAB_LAB_IMAGE", "linuxlab/lab-base:dev")


@asynccontextmanager
async def running_lab(runtime: DockerRuntime) -> AsyncIterator[ContainerInfo]:
    info = await runtime.create(LabContainerSpec(lab_id=uuid.uuid4().hex, image=LAB_IMAGE))
    try:
        await runtime.start(info.id)
        yield info
    finally:
        await runtime.remove(info.id)


async def sh(
    runtime: DockerRuntime,
    lab: ContainerInfo,
    script: str,
    *,
    user: ExecUser = "student",
    time_limit: float = 20,
) -> ExecResult:
    return await runtime.exec(lab.id, ["sh", "-c", script], user=user, time_limit=time_limit)


async def docker_inspect(client: aiodocker.Docker, lab: ContainerInfo) -> dict[str, Any]:
    return await client.containers.container(lab.id).show()
