from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from linuxlab.config import Settings, get_settings
from linuxlab.labs.runtime import LabRuntime
from linuxlab.labs.runtime.fake import FakeRuntime
from linuxlab.main import create_app
from tests.auth.support import INVITE_CODE, ORIGIN, sql
from tests.labs.support import LAB_IMAGE, OCI_RUNTIME

AppFactory = Callable[..., TestClient]


def pytest_report_header() -> str:
    return f"lab runtime: {OCI_RUNTIME}, lab image: {LAB_IMAGE}"


def app_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": get_settings().database_url,
        "allowed_origins": frozenset({ORIGIN}),
        "signup_invite_code": INVITE_CODE,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def make_client() -> Iterator[AppFactory]:
    """Start the API with the given settings on an emptied database.

    Labs run on a FakeRuntime unless `runtime` is given, and the reaper only runs if
    asked; tests call reconcile themselves. HTTPS base URL: the session cookie is
    Secure and would not be sent over http.
    """
    clients: list[TestClient] = []

    def make(
        *, runtime: LabRuntime | None = None, reaper: bool = False, **overrides: object
    ) -> TestClient:
        app = create_app(
            app_settings(**overrides), lab_runtime=runtime or FakeRuntime(), start_reaper=reaper
        )
        client = TestClient(app, base_url="https://testserver", headers={"origin": ORIGIN})
        client.__enter__()
        clients.append(client)
        sql(
            client,
            "TRUNCATE users, auth_sessions, lab_sessions, modules, missions, mission_versions",
        )
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: AppFactory) -> TestClient:
    return make_client()
