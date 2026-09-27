from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from linuxlab import health
from linuxlab.config import Settings, get_settings
from linuxlab.db import create_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        yield
        await engine.dispose()

    app = FastAPI(title="Linux Lab API", lifespan=lifespan)
    app.include_router(health.router)
    return app
