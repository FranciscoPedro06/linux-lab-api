"""Which lab a request may use.

This is the authorization boundary for anything that touches a lab: HTTP routes and
the terminal pass the authenticated user and the lab id from the URL, and get the
lab back only if that user owns it. The id alone never grants anything.
"""

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from linuxlab.labs.models import LabSession

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
