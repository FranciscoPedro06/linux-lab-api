"""Production startup against the real Docker Engine: it starts only with runsc."""

import aiodocker
import pytest
from fastapi.testclient import TestClient

from linuxlab.config import Settings
from linuxlab.main import StartupError, create_app

pytestmark = pytest.mark.docker

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://linuxlab:linuxlab@127.0.0.1:1/linuxlab"


async def test_production_starts_only_if_docker_has_runsc(
    docker_client: aiodocker.Docker,
) -> None:
    has_runsc = "runsc" in (await docker_client.system.info()).get("Runtimes", {})
    settings = Settings(environment="production", database_url=UNREACHABLE_DATABASE_URL)
    app = create_app(settings, start_reaper=False)

    def start() -> None:
        with TestClient(app) as client:
            assert client.get("/api/health").json()["runtime"] == "ok"

    if has_runsc:
        start()
    else:
        with pytest.raises(StartupError, match="runsc"):
            start()
