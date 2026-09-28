"""DockerTerminalSession.close without Docker: the stream and runtime are stand-ins."""

import asyncio
import logging
from typing import Any, cast

import pytest

from linuxlab.labs.runtime.docker import DockerRuntime, DockerTerminalSession


class FailingStream:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        raise OSError("connection to the Docker daemon was reset")


class RecordingRuntime:
    def __init__(self) -> None:
        self.cleanups: list[tuple[str, str]] = []

    async def end_terminal_processes(self, container_id: str, token: str) -> int:
        self.cleanups.append((container_id, token))
        return 0


async def test_cleanup_runs_even_if_closing_the_stream_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = FailingStream()
    runtime = RecordingRuntime()
    session = DockerTerminalSession(
        cast(DockerRuntime, runtime),
        "container-1",
        cast(Any, object()),
        cast(Any, stream),
        "token-1",
    )

    with caplog.at_level(logging.WARNING, logger="linuxlab.labs.runtime.docker"):
        async with asyncio.timeout(5):
            exit_code = await session.close()

    assert stream.close_calls == 1
    assert runtime.cleanups == [("container-1", "token-1")]
    assert exit_code is None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "did not close cleanly" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None

    # Closing again is a no-op and does not repeat the cleanup.
    assert await session.close() is None
    assert runtime.cleanups == [("container-1", "token-1")]
