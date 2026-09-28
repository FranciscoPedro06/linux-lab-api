"""WebSocket endpoint for lab terminals. See docs/terminal.md."""

import asyncio
import contextlib
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketState

from linuxlab.labs.access import LabUnavailableError
from linuxlab.labs.runtime import LabRuntimeError, TerminalSession
from linuxlab.labs.terminal.protocol import (
    INIT_TIMEOUT_SECONDS,
    CloseCode,
    Init,
    ProtocolError,
    error_message,
    exit_message,
    parse_control,
    ready_message,
)
from linuxlab.labs.terminal.relay import Outcome, RelayStats, relay

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/labs/{lab_id}/terminal")
async def terminal_endpoint(websocket: WebSocket, lab_id: str) -> None:
    state = websocket.app.state
    connection = uuid.uuid4().hex[:12]

    # Checked before accepting: browsers send Origin on WebSocket handshakes and
    # nothing else protects the socket from cross-site pages.
    if websocket.headers.get("origin") not in state.settings.allowed_origins:
        logger.warning("terminal rejected: connection=%s reason=origin", connection)
        await websocket.close()
        return
    await websocket.accept()

    # Authorization boundary: only a lab returned by lab_access is ever used.
    try:
        lab = await state.lab_access.resolve(lab_id)
    except LabUnavailableError:
        logger.info("terminal rejected: connection=%s reason=lab_unavailable", connection)
        await websocket.close(CloseCode.LAB_UNAVAILABLE)
        return

    try:
        init = await _receive_init(websocket)
    except ProtocolError as error:
        logger.info("terminal rejected: connection=%s reason=%s", connection, error)
        await _close(websocket, error.code)
        return
    if init is None:
        return

    async with state.terminals.claim(lab_id) as replaced:
        started = time.monotonic()
        try:
            terminal = await state.runtime.open_terminal(lab.id, init.size)
        except LabRuntimeError:
            logger.exception("terminal failed to start: connection=%s lab=%s", connection, lab_id)
            await _send_error_and_close(websocket, "could not start the terminal")
            return

        logger.info(
            "terminal opened: connection=%s lab=%s size=%dx%d",
            connection,
            lab_id,
            init.size.cols,
            init.size.rows,
        )
        stats = RelayStats()
        outcome: Outcome | None = None
        failure: Exception | None = None
        try:
            await websocket.send_text(ready_message())
            outcome = await relay(websocket, terminal, replaced, stats)
        except (ProtocolError, LabRuntimeError) as error:
            failure = error
        finally:
            exit_code = await _close_terminal(terminal, connection)

        reason = outcome.value if outcome else type(failure).__name__
        logger.info(
            "terminal closed: connection=%s lab=%s reason=%s exit_code=%s duration=%.1fs "
            "bytes_in=%d bytes_out=%d",
            connection,
            lab_id,
            reason,
            exit_code,
            time.monotonic() - started,
            stats.bytes_in,
            stats.bytes_out,
        )

    if isinstance(failure, ProtocolError):
        await _close(websocket, failure.code)
    elif isinstance(failure, LabRuntimeError):
        logger.error("terminal runtime error: connection=%s error=%s", connection, failure)
        await _send_error_and_close(websocket, "the terminal stopped unexpectedly")
    elif outcome is Outcome.SHELL_EXITED:
        await _send_and_close(websocket, exit_message(exit_code), CloseCode.SHELL_EXITED)
    elif outcome is Outcome.REPLACED:
        await _close(websocket, CloseCode.REPLACED)


async def _receive_init(websocket: WebSocket) -> Init | None:
    try:
        async with asyncio.timeout(INIT_TIMEOUT_SECONDS):
            message = await websocket.receive()
    except TimeoutError:
        raise ProtocolError("no init message") from None
    if message["type"] == "websocket.disconnect":
        return None
    text = message.get("text")
    if text is None:
        raise ProtocolError("expected init")
    control = parse_control(text)
    if not isinstance(control, Init):
        raise ProtocolError("expected init")
    return control


async def _close_terminal(terminal: TerminalSession, connection: str) -> int | None:
    try:
        return await terminal.close()
    except LabRuntimeError:
        logger.exception("terminal cleanup failed: connection=%s", connection)
        return None


async def _send_error_and_close(websocket: WebSocket, reason: str) -> None:
    await _send_and_close(websocket, error_message(reason), CloseCode.SERVER_ERROR)


async def _send_and_close(websocket: WebSocket, text: str, code: int) -> None:
    if websocket.client_state is WebSocketState.CONNECTED:
        try:
            await websocket.send_text(text)
        except (OSError, RuntimeError):
            return
    await _close(websocket, code)


async def _close(websocket: WebSocket, code: int) -> None:
    if websocket.client_state is WebSocketState.CONNECTED:
        with contextlib.suppress(OSError, RuntimeError):
            await websocket.close(code)
