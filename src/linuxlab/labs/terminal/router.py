"""WebSocket endpoint for lab terminals. See docs/terminal.md."""

import asyncio
import contextlib
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import State
from starlette.websockets import WebSocketState

from linuxlab.auth import sessions
from linuxlab.auth.cookies import SESSION_COOKIE
from linuxlab.auth.tokens import MAX_TOKEN_LENGTH
from linuxlab.labs.access import owned_lab
from linuxlab.labs.lifecycle import LabGoneError, Labs
from linuxlab.labs.models import LabSession, LabStatus
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
from linuxlab.labs.terminal.relay import (
    Outcome,
    RelayStats,
    TerminalClaim,
    TerminalRegistry,
    relay,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class Rejected(Exception):
    def __init__(self, code: CloseCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code


@router.websocket("/ws/labs/{lab_id}/terminal")
async def terminal_endpoint(websocket: WebSocket, lab_id: str) -> None:
    state = websocket.app.state
    labs: Labs = state.labs
    terminals: TerminalRegistry = state.terminals
    connection = uuid.uuid4().hex[:12]

    # Checked before accepting: browsers send Origin on WebSocket handshakes and
    # nothing else protects the socket from cross-site pages.
    if websocket.headers.get("origin") not in state.settings.allowed_origins:
        logger.warning("terminal rejected: connection=%s reason=origin", connection)
        await websocket.close()
        return
    # Browsers do not expose the HTTP status of a refused handshake, so everything
    # after the origin is reported with a close code the page can act on.
    await websocket.accept()

    # Authorization boundary: the session from the cookie, then a lab that session's
    # user owns and that is ready. The lab id alone grants nothing.
    try:
        lab = await _authorize(websocket, state, lab_id)
        container = await labs.running_container(lab)
    except Rejected as rejection:
        logger.info("terminal rejected: connection=%s reason=%s", connection, rejection)
        await _close(websocket, rejection.code)
        return
    except LabGoneError:
        logger.info("terminal rejected: connection=%s reason=lab_gone", connection)
        await _close(websocket, CloseCode.LAB_ENDED)
        return
    except (SQLAlchemyError, LabRuntimeError):
        logger.exception("terminal failed to authorize: connection=%s", connection)
        await _send_error_and_close(websocket, "could not open the terminal")
        return

    try:
        init = await _receive_init(websocket)
    except ProtocolError as error:
        logger.info("terminal rejected: connection=%s reason=%s", connection, error)
        await _close(websocket, error.code)
        return
    if init is None:
        return

    outcome: Outcome | None = None
    failure: Exception | None = None
    exit_code: int | None = None
    async with terminals.claim(lab.lab_key) as claim:
        # From the claim on, ending the lab stops this connection. It may have ended
        # between the checks above and the claim.
        current = await labs.get(lab.id)
        if current is None or current.status != LabStatus.READY:
            outcome = Outcome.LAB_ENDED
        else:
            outcome, failure, exit_code = await _run_terminal(
                websocket, state, lab, container.id, init, claim, connection
            )

    if outcome is Outcome.SHELL_EXITED or isinstance(failure, LabRuntimeError):
        # The shell also ends when the lab's container dies, for example when gVisor
        # is OOM-killed. Then the lab has ended, not just the shell.
        with contextlib.suppress(LabRuntimeError, SQLAlchemyError):
            try:
                await labs.running_container(lab)
            except LabGoneError:
                outcome, failure = Outcome.LAB_ENDED, None

    if outcome is Outcome.LAB_ENDED:
        await _close(websocket, CloseCode.LAB_ENDED)
    elif isinstance(failure, ProtocolError):
        await _close(websocket, failure.code)
    elif isinstance(failure, LabRuntimeError):
        logger.error("terminal runtime error: connection=%s error=%s", connection, failure)
        await _send_error_and_close(websocket, "the terminal stopped unexpectedly")
    elif outcome is Outcome.SHELL_EXITED:
        await _send_and_close(websocket, exit_message(exit_code), CloseCode.SHELL_EXITED)
    elif outcome is Outcome.REPLACED:
        await _close(websocket, CloseCode.REPLACED)


async def _authorize(websocket: WebSocket, state: State, lab_id: str) -> LabSession:
    token = websocket.cookies.get(SESSION_COOKIE)
    if not token or len(token) > MAX_TOKEN_LENGTH:
        raise Rejected(CloseCode.NOT_AUTHENTICATED, "not_authenticated")
    async with state.sessionmaker() as db:
        active = await sessions.resolve(db, token, sessions.utcnow())
        if active is None:
            raise Rejected(CloseCode.NOT_AUTHENTICATED, "not_authenticated")
        lab = await owned_lab(db, active.user.id, lab_id)
    if lab is None:
        raise Rejected(CloseCode.LAB_UNAVAILABLE, "lab_unavailable")
    if lab.status != LabStatus.READY:
        raise Rejected(CloseCode.LAB_ENDED, f"lab_{lab.status}")
    return lab


async def _run_terminal(
    websocket: WebSocket,
    state: State,
    lab: LabSession,
    container_id: str,
    init: Init,
    claim: TerminalClaim,
    connection: str,
) -> tuple[Outcome | None, Exception | None, int | None]:
    terminals: TerminalRegistry = state.terminals
    started = time.monotonic()
    try:
        terminal = await state.runtime.open_terminal(container_id, init.size)
    except LabRuntimeError as error:
        logger.warning("terminal failed to start: connection=%s lab=%s", connection, lab.id)
        return None, error, None

    logger.info(
        "terminal opened: connection=%s lab=%s size=%dx%d",
        connection,
        lab.id,
        init.size.cols,
        init.size.rows,
    )
    stats = RelayStats()
    outcome: Outcome | None = None
    failure: Exception | None = None
    try:
        await websocket.send_text(ready_message())
        outcome = await relay(
            websocket,
            terminal,
            claim,
            stats,
            on_input=lambda: terminals.touch(lab.lab_key),
        )
    except (ProtocolError, LabRuntimeError) as error:
        failure = error
    finally:
        exit_code = await _close_terminal(terminal, connection)

    reason = outcome.value if outcome else type(failure).__name__
    logger.info(
        "terminal closed: connection=%s lab=%s reason=%s exit_code=%s duration=%.1fs "
        "bytes_in=%d bytes_out=%d",
        connection,
        lab.id,
        reason,
        exit_code,
        time.monotonic() - started,
        stats.bytes_in,
        stats.bytes_out,
    )
    return outcome, failure, exit_code


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
