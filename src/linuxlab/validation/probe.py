"""Facts about a lab's state, collected by `labctl probe` inside its container.

The request lists facts from a closed catalog (stat, read, processes), built only
from the pinned mission version and the lab's parameters. It travels on standard
input; the command line is fixed. The answer is untrusted input: it is parsed by
strict models, must answer exactly the facts asked, in order, and any anomaly
(timeout, exit status, oversized or truncated output, invalid JSON, wrong version)
fails the whole probe. A partial answer is never evaluated.

The protocol is described in lab-image/labctl.
"""

import base64
import binascii
import enum
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from linuxlab.labs.runtime import (
    ContainerNotFoundError,
    ContainerNotRunningError,
    LabRuntime,
    LabRuntimeError,
    RuntimeUnavailableError,
)

PROBE_COMMAND = ("python3", "-I", "-S", "/opt/labctl/labctl", "probe")
PROBE_VERSION = 1
PROBE_SECONDS = 10
MAX_REQUEST_BYTES = 64 * 1024
MAX_FACTS = 51
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_ARGS_BYTES = 4096
MAX_PROCESSES = 1024


# Request


@dataclass(frozen=True)
class StatFact:
    path: str
    follow: bool


@dataclass(frozen=True)
class ReadFact:
    path: str


@dataclass(frozen=True)
class ProcessesFact:
    pass


Fact = StatFact | ReadFact | ProcessesFact


def _wire(fact: Fact) -> dict[str, object]:
    if isinstance(fact, StatFact):
        return {"op": "stat", "path": fact.path, "follow": fact.follow}
    if isinstance(fact, ReadFact):
        return {"op": "read", "path": fact.path}
    return {"op": "processes"}


def request_body(facts: Sequence[Fact]) -> bytes:
    """The probe's standard input. Raises ValueError past the protocol's limits."""
    if not 1 <= len(facts) <= MAX_FACTS or len(set(facts)) != len(facts):
        raise ValueError("probe request: invalid number of facts")
    body = json.dumps(
        {"v": PROBE_VERSION, "facts": [_wire(fact) for fact in facts]}, separators=(",", ":")
    ).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("probe request: too large")
    return body


# Answer


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


Errno = Annotated[str, Field(pattern=r"^E[A-Z0-9]{1,15}$")]
Name = Annotated[str, Field(max_length=256)]
FileType = Literal[
    "file", "directory", "symlink", "fifo", "socket", "char_device", "block_device", "other"
]


def _decode(value: str, limit: int) -> bytes:
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid base64") from None
    if len(data) > limit:
        raise ValueError("decoded value too large")
    return data


class StatResult(_Strict):
    op: Literal["stat"]
    path: str
    follow: bool
    status: Literal["ok", "not_found", "symlink_in_path", "loop", "error"]
    type: FileType | None = None
    mode: int | None = Field(default=None, ge=0, le=0o7777)
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)
    user: Name | None = None
    group: Name | None = None
    errno: Errno | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        described = (self.type, self.mode, self.uid, self.gid)
        if (self.status == "ok") != all(value is not None for value in described):
            raise ValueError("stat: fields do not match the status")
        if self.status != "ok" and (self.user is not None or self.group is not None):
            raise ValueError("stat: fields do not match the status")
        if (self.status == "error") != (self.errno is not None):
            raise ValueError("stat: errno does not match the status")
        return self


class ReadResult(_Strict):
    op: Literal["read"]
    path: str
    status: Literal["ok", "not_found", "symlink_in_path", "wrong_type", "too_large", "error"]
    content: str | None = None
    errno: Errno | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if (self.status == "ok") != (self.content is not None):
            raise ValueError("read: content does not match the status")
        if self.content is not None:
            _decode(self.content, MAX_READ_BYTES)
        if (self.status == "error") != (self.errno is not None):
            raise ValueError("read: errno does not match the status")
        return self

    @property
    def data(self) -> bytes:
        assert self.content is not None
        return _decode(self.content, MAX_READ_BYTES)


class ProcessInfo(_Strict):
    pid: int = Field(ge=1)
    uid: int = Field(ge=0)
    user: Name | None
    comm: str = Field(max_length=64)
    args: str

    @model_validator(mode="after")
    def _args(self) -> Self:
        _decode(self.args, MAX_ARGS_BYTES)
        return self

    @property
    def command_line(self) -> str:
        """The arguments separated by spaces, as `ps -o args` shows them."""
        raw = _decode(self.args, MAX_ARGS_BYTES)
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return " ".join(part.decode("utf-8", "replace") for part in parts)


class ProcessesResult(_Strict):
    op: Literal["processes"]
    status: Literal["ok", "too_many", "error"]
    processes: list[ProcessInfo] | None = Field(default=None, max_length=MAX_PROCESSES)
    errno: Errno | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if (self.status == "ok") != (self.processes is not None):
            raise ValueError("processes: list does not match the status")
        if (self.status == "error") != (self.errno is not None):
            raise ValueError("processes: errno does not match the status")
        return self


FactResult = Annotated[StatResult | ReadResult | ProcessesResult, Field(discriminator="op")]


class ProbeAnswer(_Strict):
    v: Literal[1]
    facts: list[FactResult] = Field(max_length=MAX_FACTS)


def _answers(fact: Fact, result: StatResult | ReadResult | ProcessesResult) -> bool:
    if isinstance(fact, StatFact):
        return (
            isinstance(result, StatResult)
            and result.path == fact.path
            and result.follow == fact.follow
        )
    if isinstance(fact, ReadFact):
        return isinstance(result, ReadResult) and result.path == fact.path
    return isinstance(result, ProcessesResult)


def parse_answer(
    facts: Sequence[Fact], output: bytes
) -> dict[Fact, StatResult | ReadResult | ProcessesResult]:
    """The answer to `facts`. Raises ValueError unless it answers each, in order."""
    try:
        answer = ProbeAnswer.model_validate_json(output)
    except ValidationError:
        raise ValueError("probe answer: invalid") from None
    if len(answer.facts) != len(facts):
        raise ValueError("probe answer: wrong number of facts")
    for fact, result in zip(facts, answer.facts, strict=True):
        if not _answers(fact, result):
            raise ValueError("probe answer: facts do not match the request")
    return dict(zip(facts, answer.facts, strict=True))


# Running the probe


class ProbeErrorCode(enum.StrEnum):
    TIMEOUT = "probe_timeout"
    FAILED = "probe_failed"
    INVALID_OUTPUT = "invalid_probe_output"
    CONTAINER_GONE = "container_gone"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"


class ProbeError(Exception):
    """The probe gave no usable answer. Carries no output from the lab."""

    def __init__(self, code: ProbeErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


Facts = Mapping[Fact, StatResult | ReadResult | ProcessesResult]


async def run_probe(runtime: LabRuntime, container_id: str, facts: Sequence[Fact]) -> Facts:
    """Collect `facts` with one `labctl probe` run as root in the container.

    Standard error is ignored and nothing from the output is logged.
    """
    body = request_body(facts)
    try:
        result = await runtime.exec(
            container_id, PROBE_COMMAND, user="root", time_limit=PROBE_SECONDS, stdin=body
        )
    except (ContainerNotRunningError, ContainerNotFoundError):
        raise ProbeError(ProbeErrorCode.CONTAINER_GONE) from None
    except RuntimeUnavailableError:
        raise ProbeError(ProbeErrorCode.RUNTIME_UNAVAILABLE) from None
    except LabRuntimeError:
        raise ProbeError(ProbeErrorCode.FAILED) from None
    if result.timed_out:
        raise ProbeError(ProbeErrorCode.TIMEOUT)
    if result.exit_code != 0:
        raise ProbeError(ProbeErrorCode.FAILED)
    if result.truncated or len(result.stdout) > MAX_OUTPUT_BYTES:
        raise ProbeError(ProbeErrorCode.INVALID_OUTPUT)
    try:
        return parse_answer(facts, result.stdout)
    except ValueError:
        raise ProbeError(ProbeErrorCode.INVALID_OUTPUT) from None
