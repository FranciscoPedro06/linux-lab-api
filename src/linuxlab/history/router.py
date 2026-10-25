"""GET /api/labs/{id}/history. See docs/api.md#get-apilabsidhistory.

Only reads. Entries are written by the terminal relay alone (linuxlab.history.recorder);
no route accepts a command from the client.
"""

from datetime import datetime
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel

from linuxlab.api import ApiError, api_router
from linuxlab.auth.router import CurrentUser, Database
from linuxlab.history.service import read_page
from linuxlab.labs.access import owned_lab

router = api_router(prefix="/api/labs")

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 500
# Largest value of a PostgreSQL integer, the type of seq.
MAX_SEQ = 2**31 - 1


class HistoryEntry(BaseModel):
    seq: int
    command: str
    # When the API observed the line, not when the command ran.
    recorded_at: datetime


class HistoryPage(BaseModel):
    entries: list[HistoryEntry]
    # Whether entries exist after the last one returned.
    more: bool


@router.get("/{lab_id}/history")
async def lab_history(
    lab_id: str,
    user: CurrentUser,
    db: Database,
    after: Annotated[int, Query(ge=0, le=MAX_SEQ)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> HistoryPage:
    lab = await owned_lab(db, user.id, lab_id)
    if lab is None:
        raise ApiError(404, "lab_not_found", "Laboratório não encontrado.")
    page = await read_page(db, lab.id, after, limit)
    return HistoryPage(
        entries=[
            HistoryEntry(seq=entry.seq, command=entry.command, recorded_at=entry.recorded_at)
            for entry in page.entries
        ],
        more=page.more,
    )
