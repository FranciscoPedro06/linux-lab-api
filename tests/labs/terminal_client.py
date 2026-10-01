"""Runs the API on a real uvicorn server and talks to it with a WebSocket client."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Origin

from linuxlab.auth.cookies import SESSION_COOKIE

ORIGIN = "http://localhost:5173"


@asynccontextmanager
async def serve(app: FastAPI) -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def terminal(
    base_url: str, lab_id: str, token: str | None, *, origin: str | None = ORIGIN
) -> AsyncIterator[ClientConnection]:
    """Open the lab's terminal WebSocket with the given session token, or none."""
    headers = {"cookie": f"{SESSION_COOKIE}={token}"} if token is not None else None
    async with connect(
        f"{base_url}/ws/labs/{lab_id}/terminal",
        origin=Origin(origin) if origin else None,
        additional_headers=headers,
        open_timeout=10,
    ) as websocket:
        yield websocket


async def start(websocket: ClientConnection, cols: int = 80, rows: int = 24) -> None:
    await websocket.send(json.dumps({"type": "init", "cols": cols, "rows": rows}))
    assert await next_control(websocket) == {"type": "ready"}


async def next_control(websocket: ClientConnection, seconds: float = 10) -> dict[str, Any]:
    async with asyncio.timeout(seconds):
        while True:
            message = await websocket.recv()
            if isinstance(message, str):
                parsed: dict[str, Any] = json.loads(message)
                return parsed


async def read_until(websocket: ClientConnection, needle: bytes, seconds: float = 10) -> bytes:
    output = b""
    async with asyncio.timeout(seconds):
        while needle not in output:
            message = await websocket.recv()
            if isinstance(message, str):
                raise AssertionError(f"control message before {needle!r}: {message}")
            output += message
    return output


async def close_code(websocket: ClientConnection, seconds: float = 10) -> int | None:
    async with asyncio.timeout(seconds):
        await websocket.wait_closed()
    return websocket.close_code
