"""Conventions shared by every HTTP route under /api. See docs/api.md#conventions.

- Errors have the shape {"error": {"code", "message"}}, with messages in Portuguese
  and no internal details.
- Requests other than GET, HEAD and OPTIONS must come from an allowed Origin and
  declare a JSON body. This is checked by ApiRoute before the request body is read
  or any handler runs. WebSocket routes are not affected; the terminal checks
  Origin itself.
"""

import logging
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiError(Exception):
    """An error reported to the client. `message` is shown to the user."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


def error_response(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status, headers=headers
    )


_HTTP_ERRORS = {
    400: ("bad_request", "Requisição inválida."),
    401: ("not_authenticated", "Sessão inválida ou expirada. Entre novamente."),
    403: ("forbidden", "Acesso negado."),
    404: ("not_found", "Recurso não encontrado."),
    405: ("method_not_allowed", "Método não permitido."),
}


def install_error_handlers(app: FastAPI) -> None:
    async def api_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, ApiError)
        return error_response(error.status, error.code, error.message, error.headers)

    async def http_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, StarletteHTTPException)
        fallback = ("request_failed", "Não foi possível concluir a requisição.")
        code, message = _HTTP_ERRORS.get(error.status_code, fallback)
        return error_response(error.status_code, code, message, error.headers)

    async def validation_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, RequestValidationError)
        if any(item.get("type") == "json_invalid" for item in error.errors()):
            return error_response(400, "invalid_json", "O corpo da requisição não é JSON válido.")
        return error_response(422, "invalid_request", "Requisição inválida.")

    async def unexpected_error(request: Request, error: Exception) -> Response:
        # Starlette logs the traceback; the client gets nothing internal.
        return error_response(500, "internal_error", "Erro interno. Tente novamente.")

    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)


def _check_unsafe_request(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in request.app.state.settings.allowed_origins:
        logger.info("request rejected: path=%s reason=origin", request.url.path)
        raise ApiError(403, "origin_not_allowed", "Origem da requisição não permitida.")
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "A requisição deve ser enviada como JSON.")


class ApiRoute(APIRoute):
    """HTTP route that enforces the Origin and JSON rules before anything else."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if request.method not in SAFE_METHODS:
                _check_unsafe_request(request)
            return await handler(request)

        return guarded


def api_router(**kwargs: Any) -> APIRouter:
    return APIRouter(route_class=ApiRoute, **kwargs)
