"""Which lab a request may use.

This is the authorization boundary for anything that touches a lab: HTTP routes and
the terminal pass the authenticated user and the lab id from the URL, and get the
lab back only if that user owns it. The id alone never grants anything.
"""

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from linuxlab.labs.models import LabSession
from linuxlab.labs.runtime import ContainerInfo, ContainerNotFoundError, LabRuntime
from linuxlab.labs.runtime.spec import LAB_ID_LABEL, MANAGED_LABEL, container_name

# Lab ids appear in URLs in canonical UUID form only.
LAB_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def parse_lab_id(raw: str) -> uuid.UUID | None:
    if not LAB_ID_PATTERN.fullmatch(raw):
        return None
    return uuid.UUID(raw)


async def owned_lab(db: AsyncSession, user_id: uuid.UUID, raw_id: str) -> LabSession | None:
    """The lab if `user_id` owns it; None if it does not exist or belongs to someone else.

    Both cases look the same to the caller, so another user's lab ids reveal nothing.
    """
    lab_id = parse_lab_id(raw_id)
    if lab_id is None:
        return None
    lab = await db.get(LabSession, lab_id, populate_existing=True)
    if lab is None or lab.user_id != user_id:
        return None
    return lab


# Development terminal access, removed together with DEV_TERMINAL_ACCESS once the
# terminal is authenticated.

DEV_LAB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class LabUnavailableError(Exception):
    pass


class DevelopmentLabAccess:
    """Grants any running lab created by the platform to whoever knows its id."""

    def __init__(self, runtime: LabRuntime) -> None:
        self._runtime = runtime

    async def resolve(self, lab_id: str) -> ContainerInfo:
        if not DEV_LAB_ID_PATTERN.fullmatch(lab_id):
            raise LabUnavailableError(lab_id)
        try:
            info = await self._runtime.inspect(container_name(lab_id))
        except ContainerNotFoundError:
            raise LabUnavailableError(lab_id) from None
        managed = info.labels.get(MANAGED_LABEL) == "true"
        if not (managed and info.labels.get(LAB_ID_LABEL) == lab_id and info.running):
            raise LabUnavailableError(lab_id)
        return info
