import json
import time

import pytest

from linuxlab.labs.runtime import TerminalSize
from linuxlab.labs.terminal.protocol import (
    MAX_CONTROL_BYTES,
    CloseCode,
    Init,
    ProtocolError,
    Resize,
    parse_control,
)
from linuxlab.labs.terminal.relay import OutputPacer, TerminalRegistry


def test_parses_init_and_resize() -> None:
    assert parse_control('{"type": "init", "cols": 80, "rows": 24}') == Init(TerminalSize(80, 24))
    assert parse_control('{"type": "resize", "cols": 1, "rows": 1}') == Resize(TerminalSize(1, 1))


@pytest.mark.parametrize(
    "message",
    [
        "not json",
        "[]",
        '{"type": "resize", "cols": 80}',
        '{"type": "resize", "cols": 80, "rows": 24, "user": "root"}',
        '{"type": "exec", "cols": 80, "rows": 24}',
        '{"type": "resize", "cols": "80", "rows": 24}',
        '{"type": "resize", "cols": true, "rows": 24}',
        '{"type": "resize", "cols": 80.0, "rows": 24}',
        '{"type": "resize", "cols": 0, "rows": 24}',
        '{"type": "resize", "cols": 100000, "rows": 24}',
        '{"type": "resize", "cols": 80, "rows": 1000}',
    ],
)
def test_rejects_invalid_control_messages(message: str) -> None:
    with pytest.raises(ProtocolError) as error:
        parse_control(message)
    assert error.value.code == CloseCode.POLICY_VIOLATION


def test_rejects_oversized_control_messages() -> None:
    message = json.dumps({"type": "resize", "cols": 80, "rows": 24, "pad": "x" * MAX_CONTROL_BYTES})

    with pytest.raises(ProtocolError) as error:
        parse_control(message)
    assert error.value.code == CloseCode.MESSAGE_TOO_BIG


async def test_output_pacer_allows_a_burst_then_holds_the_rate() -> None:
    pacer = OutputPacer(bytes_per_second=10_000)

    started = time.monotonic()
    await pacer.consume(10_000)
    burst = time.monotonic() - started
    await pacer.consume(5_000)
    paced = time.monotonic() - started

    assert burst < 0.1
    assert 0.4 < paced < 1.0


async def test_registry_replaces_the_previous_connection() -> None:
    registry = TerminalRegistry()

    async with registry.claim("lab") as first:
        async with registry.claim("lab") as second:
            assert first.is_set()
            assert not second.is_set()
        assert registry.active() == 0
    assert registry.active() == 0
