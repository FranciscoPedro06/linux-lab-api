from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from linuxlab.config import Settings, get_settings
from linuxlab.main import create_app

from .support import INVITE_CODE, ORIGIN, sql

AppFactory = Callable[..., TestClient]


def auth_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": get_settings().database_url,
        "allowed_origins": frozenset({ORIGIN}),
        "signup_invite_code": INVITE_CODE,
        "dev_terminal_access": False,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def make_client() -> Iterator[AppFactory]:
    """Start the API with the given settings on an emptied database.

    HTTPS base URL: the session cookie is Secure and would not be sent over http.
    """
    clients: list[TestClient] = []

    def make(**overrides: object) -> TestClient:
        client = TestClient(
            create_app(auth_settings(**overrides)),
            base_url="https://testserver",
            headers={"origin": ORIGIN},
        )
        client.__enter__()
        clients.append(client)
        sql(client, "TRUNCATE users, auth_sessions")
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: AppFactory) -> TestClient:
    return make_client()
