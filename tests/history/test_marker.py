"""MarkerFilter: markers are taken out of the output, everything else passes unchanged."""

import base64
import itertools
import random

import pytest

from linuxlab.history.marker import (
    MARKER_END,
    MARKER_PREFIX,
    MAX_PAYLOAD_BYTES,
    MarkerFilter,
    Rejection,
    decode_payload,
)
from linuxlab.history.models import MAX_COMMAND_BYTES


def marker(command: str | bytes) -> bytes:
    raw = command.encode() if isinstance(command, str) else command
    return MARKER_PREFIX + base64.b64encode(raw) + MARKER_END


def run(chunks: list[bytes]) -> tuple[bytes, list[str], list[Rejection]]:
    """Feed the chunks, then flush, as the relay does when the shell ends."""
    markers = MarkerFilter()
    output = b""
    commands: list[str] = []
    rejections: list[Rejection] = []
    for chunk in chunks:
        result = markers.feed(chunk)
        output += result.output
        commands += result.commands
        rejections += result.rejections
    output += markers.flush()
    assert not markers.holding
    return output, commands, rejections


def splits(data: bytes) -> list[list[bytes]]:
    """The data in one chunk, cut at every single point, and cut at random points."""
    cases = [[data]] + [[data[:index], data[index:]] for index in range(1, len(data))]
    generator = random.Random(len(data))
    for _ in range(50):
        points = sorted(generator.sample(range(1, len(data)), min(5, len(data) - 1)))
        bounds = [0, *points, len(data)]
        cases.append([data[a:b] for a, b in itertools.pairwise(bounds)])
    return cases


# Output without markers


ORDINARY_OUTPUTS = [
    b"",
    b"hello\r\n$ ",
    b"\x1b[01;32mstudent@linuxlab\x1b[00m:\x1b[01;34m~\x1b[00m$ ",
    b"\x1b]0;student@linuxlab: ~\x07$ ",  # the title set by the default prompt
    b"\x1b]7770;abc\x07",  # another OSC number
    b"\x1b]777;abc\x07",
    b"\x1b]7771abc\x07",  # no separator
    b"\x1b]7771;not base64!\x07",  # a byte outside the alphabet
    b"\x1b]7771;YWJj",  # never terminated
    b"\x1b]7771;YWJj\x1b\\",  # terminated by ST instead of BEL
    b"\x1b\x1b]\x1b]7\x1b]77\x1b]777\x1b]7771",  # repeated starts of a prefix
    bytes(range(256)) * 3,
    "ação ✓ 日本".encode(),
]


@pytest.mark.parametrize("data", ORDINARY_OUTPUTS)
def test_output_without_markers_passes_unchanged_however_it_is_split(data: bytes) -> None:
    for chunks in splits(data) if len(data) > 1 else [[data]]:
        assert run(chunks) == (data, [], [])


def test_splitting_never_changes_the_result() -> None:
    """Random output rich in marker bytes: however it is split, the result is the one
    of a single chunk, and output with nothing removed is the input itself."""
    generator = random.Random(7771)
    alphabet = b"\x1b]7771;YWJj=\x07ab\r\n"
    for _ in range(300):
        data = bytes(generator.choice(alphabet) for _ in range(48))
        whole = run([data])
        for chunks in splits(data)[:25]:
            assert run(chunks) == whole
        if not whole[1] and not whole[2]:
            assert whole[0] == data
    # Valid markers among the random bytes.
    for _ in range(100):
        pieces = [bytes(generator.choice(alphabet) for _ in range(8)) for _ in range(4)]
        data = marker("ls").join(pieces)
        whole = run([data])
        assert whole[1].count("ls") >= 3
        for chunks in splits(data)[:25]:
            assert run(chunks) == whole


def test_a_payload_longer_than_any_valid_one_is_not_a_marker() -> None:
    data = MARKER_PREFIX + b"A" * (MAX_PAYLOAD_BYTES + 4) + MARKER_END
    assert run([data]) == (data, [], [])
    assert run([data[:100], data[100:]]) == (data, [], [])


# Markers


def test_a_marker_is_removed_and_its_command_returned() -> None:
    data = b"$ ls\r\n" + marker("ls -la") + b"total 0\r\n$ "
    for chunks in splits(data):
        assert run(chunks) == (b"$ ls\r\ntotal 0\r\n$ ", ["ls -la"], [])


def test_consecutive_markers_and_repeats_are_all_returned() -> None:
    data = marker("ls") + marker("ls") + b"x" + marker("echo a; echo b && c || d")
    for chunks in splits(data):
        assert run(chunks) == (b"x", ["ls", "ls", "echo a; echo b && c || d"], [])


def test_multiline_and_unicode_commands() -> None:
    command = "for i in 1 2\ndo\techo é $i\ndone"
    assert run([marker(command)]) == (b"", [command], [])


def test_a_marker_after_a_false_start_is_found() -> None:
    data = b"\x1b]7771;not-b64" + marker("pwd") + b"\x1b"
    output, commands, _ = run([data])
    assert commands == ["pwd"]
    assert output == b"\x1b]7771;not-b64\x1b"


def test_bytes_are_held_only_while_a_marker_may_still_complete() -> None:
    markers = MarkerFilter()
    assert markers.feed(b"abc\x1b]77").output == b"abc"
    assert markers.holding
    assert markers.feed(b"0").output == b"\x1b]770"
    assert not markers.holding
    assert markers.feed(b"\x1b").output == b""
    assert markers.flush() == b"\x1b"


def test_held_bytes_are_bounded_by_one_marker() -> None:
    markers = MarkerFilter()
    markers.feed(MARKER_PREFIX + b"A" * (MAX_PAYLOAD_BYTES - 1))
    assert markers.holding
    result = markers.feed(b"AA")
    assert not markers.holding
    assert result.output == MARKER_PREFIX + b"A" * (MAX_PAYLOAD_BYTES + 1)


# Rejected content


@pytest.mark.parametrize(
    ("payload", "rejection"),
    [
        (b"YWJ", Rejection.INVALID_BASE64),  # missing padding
        (b"YW=j", Rejection.INVALID_BASE64),
        (b"", Rejection.EMPTY),
        (base64.b64encode(b"\xff\xfe"), Rejection.INVALID_UTF8),
        (base64.b64encode(b"echo \xc3"), Rejection.INVALID_UTF8),
        (base64.b64encode(b"echo \x00"), Rejection.CONTROL_CHARACTER),
        (base64.b64encode(b"echo \x1b[201~"), Rejection.CONTROL_CHARACTER),
        (base64.b64encode(b"echo \r"), Rejection.CONTROL_CHARACTER),
        (base64.b64encode(b"echo \x7f"), Rejection.CONTROL_CHARACTER),
        (base64.b64encode("echo \x9b".encode()), Rejection.CONTROL_CHARACTER),
        (base64.b64encode(b"a" * (MAX_COMMAND_BYTES + 1)), Rejection.TOO_LARGE),
    ],
)
def test_complete_markers_with_unacceptable_content_are_removed_and_rejected(
    payload: bytes, rejection: Rejection
) -> None:
    data = b"a" + MARKER_PREFIX + payload + MARKER_END + b"b"
    for chunks in splits(data)[:40]:
        assert run(chunks) == (b"ab", [], [rejection])


def test_the_longest_command_is_accepted() -> None:
    command = "é" * (MAX_COMMAND_BYTES // 2)
    assert decode_payload(base64.b64encode(command.encode())) == command
    assert run([marker(command)]) == (b"", [command], [])


def test_tab_and_newline_are_accepted() -> None:
    assert decode_payload(base64.b64encode(b"a\tb\nc")) == "a\tb\nc"
