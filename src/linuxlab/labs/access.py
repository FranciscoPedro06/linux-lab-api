"""Which lab a request may use.

This is the authorization boundary for anything that touches a lab: callers pass
a lab id, and get back a running container only if the caller may use it.
"""

import re
from typing import Protocol

from linuxlab.labs.runtime import ContainerInfo, ContainerNotFoundError, LabRuntime
from linuxlab.labs.runtime.spec import LAB_ID_LABEL, MANAGED_LABEL, container_name

LAB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class LabUnavailableError(Exception):
    pass


class LabAccess(Protocol):
    async def resolve(self, lab_id: str) -> ContainerInfo:
        """Return the running container of a lab the caller may use.

        Raises LabUnavailableError otherwise, without saying whether the lab exists.
        """


class DevelopmentLabAccess:
    """Grants any running lab created by the platform to whoever knows its id.

    Only for local development (DEV_TERMINAL_ACCESS). With authentication and lab
    sessions, this is replaced by a lookup of the caller's own active lab.
    """

    def __init__(self, runtime: LabRuntime) -> None:
        self._runtime = runtime

    async def resolve(self, lab_id: str) -> ContainerInfo:
        if not LAB_ID_PATTERN.fullmatch(lab_id):
            raise LabUnavailableError(lab_id)
        try:
            info = await self._runtime.inspect(container_name(lab_id))
        except ContainerNotFoundError:
            raise LabUnavailableError(lab_id) from None
        managed = info.labels.get(MANAGED_LABEL) == "true"
        if not (managed and info.labels.get(LAB_ID_LABEL) == lab_id and info.running):
            raise LabUnavailableError(lab_id)
        return info
