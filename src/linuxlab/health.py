import asyncio
import logging
from typing import Literal

from fastapi import Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from linuxlab.api import api_router

logger = logging.getLogger(__name__)

router = api_router()

CHECK_TIMEOUT_SECONDS = 3


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unavailable"]


@router.get("/api/health")
async def health(request: Request, response: Response) -> HealthResponse:
    engine: AsyncEngine = request.app.state.engine
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS), engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError):
        logger.warning("database health check failed", exc_info=True)
        response.status_code = 503
        return HealthResponse(status="unavailable", database="unavailable")
    return HealthResponse(status="ok", database="ok")
