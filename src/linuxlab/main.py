import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiodocker
from fastapi import FastAPI

from linuxlab import health
from linuxlab.api import install_error_handlers
from linuxlab.auth import router as auth
from linuxlab.auth.passwords import Passwords
from linuxlab.auth.ratelimit import RateLimiter
from linuxlab.catalog import router as catalog
from linuxlab.config import Settings, get_settings
from linuxlab.db import create_engine, create_sessionmaker
from linuxlab.labs import router as labs
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.reaper import run_reaper
from linuxlab.labs.router import CREATE_ATTEMPTS, CREATE_WINDOW_SECONDS
from linuxlab.labs.runtime import LabRuntime, LabRuntimeError
from linuxlab.labs.runtime.docker import DockerRuntime
from linuxlab.labs.terminal import router as terminal
from linuxlab.labs.terminal.relay import TerminalRegistry

logger = logging.getLogger("linuxlab.main")

STARTUP_CHECK_SECONDS = 10


def create_app(
    settings: Settings | None = None,
    *,
    lab_runtime: LabRuntime | None = None,
    start_reaper: bool = True,
) -> FastAPI:
    """Build the application.

    Tests replace the Docker runtime with `lab_runtime` and run reconciliation
    themselves instead of starting the reaper.
    """
    settings = settings or get_settings()
    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.passwords = Passwords()
        app.state.auth_rate_limits = {"signup": RateLimiter(), "login": RateLimiter()}
        app.state.lab_rate_limit = RateLimiter(CREATE_ATTEMPTS, CREATE_WINDOW_SECONDS)
        docker: aiodocker.Docker | None = None
        runtime = lab_runtime
        if runtime is None:
            docker = aiodocker.Docker()
            runtime = DockerRuntime(docker, oci_runtime=settings.lab_oci_runtime)
        app.state.runtime = runtime
        try:
            await _check_runtime(settings, runtime)
        except BaseException:
            if docker is not None:
                await docker.close()
            await engine.dispose()
            raise
        app.state.terminals = TerminalRegistry()
        app.state.labs = Labs(
            app.state.sessionmaker,
            runtime,
            app.state.terminals,
            image=settings.lab_image,
            deployment=settings.lab_deployment,
            capacity=settings.lab_capacity,
        )
        reaper = None
        if start_reaper:
            reaper = asyncio.create_task(run_reaper(app.state.labs), name="lab-reaper")
        try:
            yield
        finally:
            if reaper is not None:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
            if docker is not None:
                await docker.close()
            await engine.dispose()

    app = FastAPI(title="Linux Lab API", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(catalog.router)
    app.include_router(labs.router)
    app.include_router(terminal.router)
    return app


class StartupError(Exception):
    pass


async def _check_runtime(settings: Settings, runtime: LabRuntime) -> None:
    """In production, refuse to start unless Docker can run labs under gVisor.

    Settings already require LAB_OCI_RUNTIME=runsc in production; this checks that
    Docker is reachable and has runsc registered. In development an unavailable
    runtime is only reported, by this log and by /api/health.
    """
    try:
        async with asyncio.timeout(STARTUP_CHECK_SECONDS):
            await runtime.ping()
    except (LabRuntimeError, TimeoutError) as error:
        if settings.environment == "production":
            logger.critical("refusing to start: lab runtime unavailable: %s", error)
            raise StartupError(
                f"ENVIRONMENT=production requires Docker with the {settings.lab_oci_runtime} "
                f"runtime: {error or 'no answer'}"
            ) from error
        logger.warning("lab runtime unavailable: %s", error)


def _configure_logging() -> None:
    logger = logging.getLogger("linuxlab")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
