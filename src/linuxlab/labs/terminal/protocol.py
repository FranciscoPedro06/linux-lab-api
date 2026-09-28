"""Terminal WebSocket protocol. See docs/terminal.md.

Binary frames carry terminal bytes in both directions. Text frames carry small JSON
control messages.
"""

import json
from dataclasses import dataclass
from enum import IntEnum

from linuxlab.labs.runtime import TerminalSize

MAX_FRAME_BYTES = 64 * 1024
MAX_CONTROL_BYTES = 1024
INIT_TIMEOUT_SECONDS = 5
OUTPUT_BYTES_PER_SECOND = 256 * 1024


class CloseCode(IntEnum):
    POLICY_VIOLATION = 1008
    MESSAGE_TOO_BIG = 1009
    SERVER_ERROR = 1011
    SHELL_EXITED = 4000
    LAB_UNAVAILABLE = 4404
    REPLACED = 4409


class ProtocolError(Exception):
    def __init__(self, reason: str, code: CloseCode = CloseCode.POLICY_VIOLATION) -> None:
        super().__init__(reason)
        self.code = code


@dataclass(frozen=True)
class Init:
    size: TerminalSize


@dataclass(frozen=True)
class Resize:
    size: TerminalSize


def parse_control(text: str) -> Init | Resize:
    if len(text.encode()) > MAX_CONTROL_BYTES:
        raise ProtocolError("control message too large", CloseCode.MESSAGE_TOO_BIG)
    try:
        message = json.loads(text)
    except ValueError:
        raise ProtocolError("control message is not JSON") from None
    if not isinstance(message, dict) or set(message) != {"type", "cols", "rows"}:
        raise ProtocolError("unexpected control message")
    kind, cols, rows = message["type"], message["cols"], message["rows"]
    if kind not in ("init", "resize") or type(cols) is not int or type(rows) is not int:
        raise ProtocolError("unexpected control message")
    try:
        size = TerminalSize(cols=cols, rows=rows)
    except ValueError:
        raise ProtocolError("terminal size out of range") from None
    return Init(size) if kind == "init" else Resize(size)


def ready_message() -> str:
    return json.dumps({"type": "ready"})


def exit_message(code: int | None) -> str:
    return json.dumps({"type": "exit", "code": code})


def error_message(reason: str) -> str:
    return json.dumps({"type": "error", "message": reason})
