from collections.abc import AsyncIterator

import aiodocker
import pytest

from linuxlab.labs.runtime import ContainerInfo
from linuxlab.labs.runtime.docker import DockerRuntime

from .support import OCI_RUNTIME, running_lab


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if OCI_RUNTIME == "runsc":
        return
    skip = pytest.mark.skip(reason=f"requires runsc (running under {OCI_RUNTIME})")
    for item in items:
        if "runsc" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
async def docker_client() -> AsyncIterator[aiodocker.Docker]:
    client = aiodocker.Docker()
    yield client
    await client.close()


@pytest.fixture(scope="session")
async def runtime(docker_client: aiodocker.Docker) -> DockerRuntime:
    runtime = DockerRuntime(docker_client, oci_runtime=OCI_RUNTIME)
    await runtime.ping()
    return runtime


@pytest.fixture(scope="module")
async def lab(runtime: DockerRuntime) -> AsyncIterator[ContainerInfo]:
    """Container shared by tests that do not change its state in a lasting way."""
    async with running_lab(runtime) as info:
        yield info


@pytest.fixture
async def fresh_lab(runtime: DockerRuntime) -> AsyncIterator[ContainerInfo]:
    """Container for tests that exhaust a resource."""
    async with running_lab(runtime) as info:
        yield info
