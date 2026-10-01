import pytest
from fastapi.testclient import TestClient

from linuxlab.config import Settings
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.main import create_app
from tests.conftest import AppFactory

# Nothing listens on port 1, so the connection is refused immediately.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://linuxlab:linuxlab@127.0.0.1:1/linuxlab"


def unreachable_database_app(runtime: FakeRuntime) -> TestClient:
    settings = Settings(environment="development", database_url=UNREACHABLE_DATABASE_URL)
    return TestClient(create_app(settings, lab_runtime=runtime, start_reaper=False))


@pytest.mark.parametrize(("available", "runtime"), [(True, "ok"), (False, "unavailable")])
def test_health_reports_unavailable_database(available: bool, runtime: str) -> None:
    with unreachable_database_app(FakeRuntime(available=available)) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "unavailable",
        "runtime": runtime,
    }


@pytest.mark.integration
def test_health_reports_every_dependency_ok(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "runtime": "ok"}


@pytest.mark.integration
def test_health_reports_unavailable_runtime(make_client: AppFactory) -> None:
    client = make_client(runtime=FakeRuntime(available=False))

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "database": "ok",
        "runtime": "unavailable",
    }
