"""Conventions shared by /api routes: Origin and JSON checks, error format."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from linuxlab.api import ApiError, api_router, install_error_handlers
from linuxlab.config import Settings

ORIGIN = "http://localhost:5173"

calls: list[str] = []


class Echo(BaseModel):
    value: str


def build_app() -> FastAPI:
    router = api_router(prefix="/api")

    @router.post("/echo")
    async def echo(body: Echo) -> Echo:
        calls.append(body.value)
        return body

    @router.get("/fail")
    async def fail() -> None:
        raise ApiError(409, "conflict_here", "Conflito.")

    @router.get("/crash")
    async def crash() -> None:
        raise RuntimeError("SELECT secret FROM internal")

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.state.settings = Settings.model_validate(
        {"database_url": "postgresql+asyncpg://unused", "allowed_origins": frozenset({ORIGIN})}
    )
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    calls.clear()
    with TestClient(build_app(), raise_server_exceptions=False) as client:
        yield client


def post(client: TestClient, headers: dict[str, str], content: bytes = b'{"value": "x"}') -> Any:
    return client.post("/api/echo", content=content, headers=headers)


def test_valid_origin_and_json_reach_the_handler(client: TestClient) -> None:
    response = post(client, {"origin": ORIGIN, "content-type": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"value": "x"}
    assert calls == ["x"]


def test_json_content_type_with_charset_is_accepted(client: TestClient) -> None:
    response = post(client, {"origin": ORIGIN, "content-type": "application/json; charset=utf-8"})

    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "application/json"},
        {"origin": "https://evil.example", "content-type": "application/json"},
        {"origin": "null", "content-type": "application/json"},
        {"origin": ORIGIN.upper(), "content-type": "application/json"},
    ],
    ids=["missing", "other-site", "null", "case-changed"],
)
def test_origin_not_allowed(client: TestClient, headers: dict[str, str]) -> None:
    response = post(client, headers)

    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "origin_not_allowed", "message": "Origem da requisição não permitida."}
    }
    assert calls == []


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"],
)
def test_body_must_be_declared_json(client: TestClient, content_type: str | None) -> None:
    headers = {"origin": ORIGIN}
    if content_type:
        headers["content-type"] = content_type

    response = post(client, headers)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"
    assert calls == []


def test_origin_is_checked_before_the_body_is_parsed(client: TestClient) -> None:
    response = post(client, {"content-type": "application/json"}, b"{not json")

    assert response.status_code == 403


def test_malformed_json(client: TestClient) -> None:
    response = post(client, {"origin": ORIGIN, "content-type": "application/json"}, b"{not json")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_schema_mismatch(client: TestClient) -> None:
    response = post(client, {"origin": ORIGIN, "content-type": "application/json"}, b'{"v": 1}')

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Requisição inválida."}
    }


def test_get_needs_no_origin(client: TestClient) -> None:
    response = client.get("/api/fail")

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "conflict_here", "message": "Conflito."}}


def test_unknown_route_and_method(client: TestClient) -> None:
    assert client.get("/api/nothing").json()["error"]["code"] == "not_found"
    assert client.get("/api/echo").json()["error"]["code"] == "method_not_allowed"


def test_unexpected_errors_reveal_nothing(client: TestClient) -> None:
    response = client.get("/api/crash")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Erro interno. Tente novamente."}
    }
    assert "SELECT" not in response.text
