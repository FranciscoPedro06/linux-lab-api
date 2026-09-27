import pytest
from fastapi.testclient import TestClient

from linuxlab.config import Settings, get_settings
from linuxlab.main import create_app

# Nothing listens on port 1, so the connection is refused immediately.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://linuxlab:linuxlab@127.0.0.1:1/linuxlab"


def test_health_reports_unavailable_database() -> None:
    app = create_app(Settings(database_url=UNREACHABLE_DATABASE_URL))

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


@pytest.mark.integration
def test_health_reports_available_database() -> None:
    app = create_app(get_settings())

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
