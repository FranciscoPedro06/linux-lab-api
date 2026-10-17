"""How DockerRuntime.exec reports a container removed while the exec runs.

No Docker: the client is replaced by stand-ins. Once the container is gone, Docker no
longer knows the exec instance, and reading its exit code answers 404; the runtime
must report that as the container being gone, not let Docker's error escape (the
terminal's cleanup and the validation probe both rely on it).
"""

from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from aiodocker.exceptions import DockerError

from linuxlab.labs.runtime import ContainerNotFoundError, LabRuntimeError
from linuxlab.labs.runtime.docker import DockerRuntime


class Stream:
    async def read_out(self) -> None:
        return None

    async def __aenter__(self) -> "Stream":
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        pass


class Execution:
    def __init__(self, status: int) -> None:
        self.status = status

    def start(self, detach: bool) -> Stream:
        return Stream()

    async def inspect(self) -> dict[str, Any]:
        raise DockerError(self.status, "No such exec instance: abc")


def runtime(status: int) -> DockerRuntime:
    async def show() -> dict[str, Any]:
        return {"Id": "c1", "Name": "/ll-lab-x", "State": {"Running": True}, "Config": {}}

    async def exec_(**kwargs: Any) -> Execution:
        return Execution(status)

    container = SimpleNamespace(show=show, exec=exec_)
    client = SimpleNamespace(containers=SimpleNamespace(container=lambda _id: container))
    return DockerRuntime(cast(Any, client), oci_runtime="runc")


async def test_an_exec_whose_container_disappeared_reports_it_gone() -> None:
    with pytest.raises(ContainerNotFoundError):
        await runtime(404).exec("c1", ["true"], user="root", time_limit=5)


async def test_other_docker_errors_while_waiting_are_runtime_errors() -> None:
    with pytest.raises(LabRuntimeError):
        await runtime(500).exec("c1", ["true"], user="root", time_limit=5)
