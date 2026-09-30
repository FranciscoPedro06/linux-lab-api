import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiodocker
from fastapi import FastAPI

from linuxlab import health
from linuxlab.api import install_error_handlers
from linuxlab.config import Settings, get_settings
from linuxlab.db import create_engine
from linuxlab.labs.access import DevelopmentLabAccess
from linuxlab.labs.runtime import LabRuntime
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.terminal import router as terminal
from linuxlab.labs.terminal.relay import TerminalRegistry


def create_app(
    settings: Settings | None = None, *, lab_runtime: LabRuntime | None = None
) -> FastAPI:
    """Build the application. `lab_runtime` replaces the Docker runtime in tests."""
    settings = settings or get_settings()
    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        docker: aiodocker.Docker | None = None
        if settings.dev_terminal_access:
            runtime = lab_runtime
            if runtime is None:
                docker = aiodocker.Docker()
                runtime = DockerRuntime(docker, oci_runtime=settings.lab_oci_runtime)
            app.state.runtime = runtime
            app.state.lab_access = DevelopmentLabAccess(runtime)
            app.state.terminals = TerminalRegistry()
        try:
            yield
        finally:
            if docker is not None:
                await docker.close()
            await engine.dispose()

    app = FastAPI(title="Linux Lab API", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    if settings.dev_terminal_access:
        app.include_router(terminal.router)
    return app


def _configure_logging() -> None:
    logger = logging.getLogger("linuxlab")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
