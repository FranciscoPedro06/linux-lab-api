import asyncio
import logging
from typing import Literal

from fastapi import Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from linuxlab.api import api_router
from linuxlab.labs.runtime import LabRuntime, LabRuntimeError

logger = logging.getLogger(__name__)

router = api_router()

CHECK_TIMEOUT_SECONDS = 3

Status = Literal["ok", "unavailable"]


class HealthResponse(BaseModel):
    """Only states: no versions, addresses, socket paths or error details."""

    status: Status
    database: Status
    runtime: Status


@router.get("/api/health")
async def health(request: Request, response: Response) -> HealthResponse:
    database, runtime = await asyncio.gather(
        _database(request.app.state.engine), _runtime(request.app.state.runtime)
    )
    healthy = database == runtime == "ok"
    if not healthy:
        response.status_code = 503
    return HealthResponse(
        status="ok" if healthy else "unavailable", database=database, runtime=runtime
    )


async def _database(engine: AsyncEngine) -> Status:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError):
        logger.warning("database health check failed", exc_info=True)
        return "unavailable"
    return "ok"


async def _runtime(runtime: LabRuntime) -> Status:
    """Docker is reachable and has the configured OCI runtime (runsc in production)."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await runtime.ping()
    except (LabRuntimeError, TimeoutError) as error:
        logger.warning("lab runtime health check failed: %s", error)
        return "unavailable"
    return "ok"
