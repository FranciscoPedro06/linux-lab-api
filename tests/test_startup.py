"""Production refuses to start without gVisor; development starts and reports it."""

import pytest
from fastapi.testclient import TestClient

from linuxlab.config import Settings
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.main import StartupError, create_app

UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://linuxlab:linuxlab@127.0.0.1:1/linuxlab"


def settings(environment: str) -> Settings:
    return Settings.model_validate(
        {"environment": environment, "database_url": UNREACHABLE_DATABASE_URL}
    )


def test_production_does_not_start_without_the_runtime(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(settings("production"), lab_runtime=FakeRuntime(available=False))

    with (
        pytest.raises(StartupError, match="requires Docker with the runsc runtime"),
        TestClient(app),
    ):
        pass
    assert any("refusing to start" in record.getMessage() for record in caplog.records)


def test_production_starts_when_the_runtime_answers() -> None:
    app = create_app(settings("production"), lab_runtime=FakeRuntime(), start_reaper=False)

    with TestClient(app) as client:
        assert client.get("/api/health").json()["runtime"] == "ok"


def test_development_starts_without_the_runtime() -> None:
    app = create_app(
        settings("development"), lab_runtime=FakeRuntime(available=False), start_reaper=False
    )

    with TestClient(app) as client:
        assert client.get("/api/health").json()["runtime"] == "unavailable"
