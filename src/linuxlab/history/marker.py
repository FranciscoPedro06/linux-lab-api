"""The private marker a lab's shell writes for each command line it accepts.

    ESC ] 7771 ; <payload> BEL

The payload is the command line, as bash recorded it in its own history, encoded in
UTF-8 and then in standard base64 with padding. It is written to the terminal by the
hook in lab-image/history.bash, from PS0: after bash accepted the line and before it
runs. The shape is that of an OSC (operating system command) sequence, with a number
no terminal assigns.

MarkerFilter removes markers from the terminal output before it reaches the browser
and returns the command lines they carried. Every other byte is passed on unchanged
and in order. Anything that does not complete the marker grammar (a byte outside the
base64 alphabet before BEL, or a payload longer than any valid one) is not a marker
and is passed on as it is. A complete marker whose content is not acceptable is
removed and reported as a rejection, never as a command.

A marker can be split across output chunks, so a chunk that ends in what may be the
start of one is held back until the next chunk decides. At most one marker's worth
of bytes is ever held, and the relay passes held bytes on unchanged when no output
follows within HOLD_SECONDS, so output is never delayed for long.

Anything running in the lab can write a marker, so a command line obtained here is
what the lab's output claims, not proof that a command ran.
"""

import base64
import binascii
import enum
import re
from dataclasses import dataclass, field

from linuxlab.history.models import MAX_COMMAND_BYTES

MARKER_PREFIX = b"\x1b]7771;"
MARKER_END = b"\x07"
# Base64 of the longest command accepted.
MAX_PAYLOAD_BYTES = 4 * ((MAX_COMMAND_BYTES + 2) // 3)
# How long the relay waits for the rest of a possible marker before passing on the
# bytes it holds.
HOLD_SECONDS = 0.2

_PAYLOAD = re.compile(rb"[A-Za-z0-9+/=]*")
# C0 controls other than tab and newline, DEL and C1 controls.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Rejection(enum.StrEnum):
    INVALID_BASE64 = "invalid_base64"
    INVALID_UTF8 = "invalid_utf8"
    EMPTY = "empty"
    TOO_LARGE = "too_large"
    CONTROL_CHARACTER = "control_character"


@dataclass
class Filtered:
    # Output to pass on, in order.
    output: bytes
    commands: list[str] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


def decode_payload(payload: bytes) -> str | Rejection:
    try:
        raw = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return Rejection.INVALID_BASE64
    if not raw:
        return Rejection.EMPTY
    if len(raw) > MAX_COMMAND_BYTES:
        return Rejection.TOO_LARGE
    try:
        command = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return Rejection.INVALID_UTF8
    if _CONTROL_CHARACTERS.search(command):
        return Rejection.CONTROL_CHARACTER
    return command


class MarkerFilter:
    def __init__(self) -> None:
        self._held = b""

    @property
    def holding(self) -> bool:
        """Whether bytes are held back, waiting for the next chunk."""
        return bool(self._held)

    def feed(self, chunk: bytes) -> Filtered:
        data = self._held + chunk
        self._held = b""
        result = Filtered(b"")
        output = bytearray()
        position = 0
        while True:
            start = data.find(MARKER_PREFIX, position)
            if start < 0:
                # The data may end with the first bytes of a prefix.
                kept = _prefix_at_end(data, position)
                output += data[position : len(data) - kept]
                self._held = data[len(data) - kept :]
                break
            output += data[position:start]
            payload_start = start + len(MARKER_PREFIX)
            match = _PAYLOAD.match(data, payload_start, payload_start + MAX_PAYLOAD_BYTES)
            assert match is not None  # the pattern also matches an empty payload
            payload_end = match.end()
            if payload_end == len(data):
                # The data ends inside the payload.
                self._held = data[start:]
                break
            if data[payload_end : payload_end + 1] != MARKER_END:
                # Not a marker: its first byte is passed on, and the rest scanned again.
                output += data[start : start + 1]
                position = start + 1
                continue
            decoded = decode_payload(data[payload_start:payload_end])
            if isinstance(decoded, Rejection):
                result.rejections.append(decoded)
            else:
                result.commands.append(decoded)
            position = payload_end + 1
        result.output = bytes(output)
        return result

    def flush(self) -> bytes:
        """Give up on the held bytes: they are passed on as output."""
        held, self._held = self._held, b""
        return held


def _prefix_at_end(data: bytes, position: int) -> int:
    """How many bytes at the end of data[position:] are the start of a prefix."""
    for size in range(min(len(MARKER_PREFIX) - 1, len(data) - position), 0, -1):
        if data.endswith(MARKER_PREFIX[:size]):
            return size
    return 0
