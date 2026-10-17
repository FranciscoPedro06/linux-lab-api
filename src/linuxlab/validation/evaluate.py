"""Evaluating a resolved condition tree against collected facts. Pure: no I/O.

Every leaf ends `passed`, `failed` or `error`:

- `failed` is the lab's state not matching, with a reason from a closed set
  (Reason). It covers states the student caused: a missing path, a symbolic link or
  a FIFO where a file is expected, a file too large or not UTF-8, no answer.
- `error` is the check itself not running: the probe gave no answer, or could not
  read this fact. An error never becomes `failed` or `passed`.

Operators combine their children (docs/architecture.md#validation):

- all: failed if any child failed; otherwise error if any errored; otherwise passed;
- any: passed if any child passed; otherwise error if any errored; otherwise failed;
- not: swaps passed and failed; error stays error.

Every leaf is evaluated, with no short-circuiting, so each condition gets a result.
Nothing observed in the lab (file contents, command lines) and nothing expected
(values, parameters) is kept in the result: only statuses and reasons.
"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field

import re2
from pydantic import BaseModel

from linuxlab.content.schema import (
    AllOf,
    Answer,
    AnyOf,
    DirectoryExists,
    FileContent,
    FileExists,
    FileGroup,
    FileOwner,
    FilePermissions,
    NotOf,
    PathAbsent,
    ProcessNotRunning,
    ProcessRunning,
)
from linuxlab.validation.plan import Plan, fact_for
from linuxlab.validation.probe import (
    Facts,
    ProcessesResult,
    ProcessInfo,
    ReadResult,
    StatResult,
)


class Status(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class Reason(enum.StrEnum):
    NOT_FOUND = "not_found"
    WRONG_TYPE = "wrong_type"
    TOO_LARGE = "too_large"
    NOT_UTF8 = "not_utf8"
    MISMATCH = "mismatch"
    NO_ANSWER = "no_answer"


@dataclass(frozen=True)
class Node:
    """A node of the result. `kind` is `all`, `any`, `not` or `check`."""

    kind: str
    status: Status
    # Checks only.
    id: str | None = None
    type: str | None = None
    reason: Reason | None = None
    # The message from the mission (trusted content) or a default; never derived
    # from the lab's state.
    message: str | None = None
    children: tuple["Node", ...] = field(default_factory=tuple)


PASSED_MESSAGE = "Condição atendida."
FAILED_MESSAGE = "Condição ainda não atendida."
NO_ANSWER_MESSAGE = "Envie uma resposta para verificar esta condição."
ERROR_MESSAGE = "Não foi possível verificar esta condição."

_PASSED = (Status.PASSED, None)


def _failed(reason: Reason) -> tuple[Status, Reason | None]:
    return Status.FAILED, reason


_ERROR: tuple[Status, Reason | None] = (Status.ERROR, None)


def _matches(ok: bool) -> tuple[Status, Reason | None]:
    return _PASSED if ok else _failed(Reason.MISMATCH)


def _stat_outcome(result: StatResult) -> tuple[Status, Reason | None] | None:
    """The outcome for a stat that did not describe a path, or None when it did."""
    if result.status == "ok":
        return None
    if result.status == "not_found":
        return _failed(Reason.NOT_FOUND)
    if result.status in ("symlink_in_path", "loop"):
        return _failed(Reason.WRONG_TYPE)
    return _ERROR


def _exists(result: StatResult, kind: str) -> tuple[Status, Reason | None]:
    outcome = _stat_outcome(result)
    if outcome is not None:
        return outcome
    return _PASSED if result.type == kind else _failed(Reason.WRONG_TYPE)


def _absent(result: StatResult) -> tuple[Status, Reason | None]:
    if result.status == "not_found":
        return _PASSED
    if result.status == "ok":
        return _failed(Reason.MISMATCH)
    if result.status == "symlink_in_path":
        return _failed(Reason.WRONG_TYPE)
    return _ERROR


def _attribute(check: BaseModel, result: StatResult) -> tuple[Status, Reason | None]:
    outcome = _stat_outcome(result)
    if outcome is not None:
        return outcome
    # Symbolic links are never followed: the link's own mode and owner say nothing
    # about what it points to.
    if result.type == "symlink":
        return _failed(Reason.WRONG_TYPE)
    if isinstance(check, FilePermissions):
        return _matches(result.mode == int(check.mode, 8))
    if isinstance(check, FileOwner):
        return _matches(result.user == check.user)
    assert isinstance(check, FileGroup)
    return _matches(result.group == check.group)


def _content(
    check: FileContent, result: ReadResult, pattern: re2._Regexp | None
) -> tuple[Status, Reason | None]:
    if result.status == "not_found":
        return _failed(Reason.NOT_FOUND)
    if result.status in ("symlink_in_path", "wrong_type"):
        return _failed(Reason.WRONG_TYPE)
    if result.status == "too_large":
        return _failed(Reason.TOO_LARGE)
    if result.status != "ok":
        return _ERROR
    try:
        text = result.data.decode("utf-8")
    except UnicodeDecodeError:
        return _failed(Reason.NOT_UTF8)
    # trim applies to what was observed only; the expected value and the pattern are
    # used as written.
    if check.trim:
        text = text.strip()
    if check.equals is not None:
        return _matches(text == check.equals)
    if check.contains is not None:
        return _matches(check.contains in text)
    if check.not_contains is not None:
        return _matches(check.not_contains not in text)
    assert pattern is not None
    return _matches(pattern.search(text) is not None)


def _process_matches(
    check: ProcessRunning | ProcessNotRunning, process: ProcessInfo, pattern: re2._Regexp | None
) -> bool:
    if check.user is not None and process.user != check.user:
        return False
    if check.match.comm is not None:
        return process.comm == check.match.comm
    assert pattern is not None
    return pattern.search(process.command_line) is not None


def _processes(
    check: ProcessRunning | ProcessNotRunning,
    result: ProcessesResult,
    pattern: re2._Regexp | None,
) -> tuple[Status, Reason | None]:
    if result.status != "ok" or result.processes is None:
        return _ERROR
    count = sum(1 for process in result.processes if _process_matches(check, process, pattern))
    if isinstance(check, ProcessRunning):
        return _matches(count >= check.min_count)
    return _matches(count == 0)


def normalize(value: str, options: Sequence[str]) -> str:
    """`trim`, then `lowercase`, whatever order the mission lists them in."""
    if "trim" in options:
        value = value.strip()
    if "lowercase" in options:
        value = value.lower()
    return value


def _answer(check: Answer, answer: str | None) -> tuple[Status, Reason | None]:
    if answer is None:
        return _failed(Reason.NO_ANSWER)
    given = normalize(answer, check.normalize)
    if check.one_of is not None:
        return _matches(any(given == normalize(item, check.normalize) for item in check.one_of))
    assert check.equals is not None
    return _matches(given == normalize(check.equals, check.normalize))


def evaluate_check(
    check: BaseModel,
    facts: Facts | None,
    pattern: re2._Regexp | None,
    answer: str | None,
) -> tuple[Status, Reason | None]:
    """One leaf. `facts` is None when the probe gave no answer."""
    if isinstance(check, Answer):
        return _answer(check, answer)
    fact = fact_for(check)
    if facts is None or fact is None or fact not in facts:
        return _ERROR
    result = facts[fact]
    if isinstance(check, FileExists) and isinstance(result, StatResult):
        return _exists(result, "file")
    if isinstance(check, DirectoryExists) and isinstance(result, StatResult):
        return _exists(result, "directory")
    if isinstance(check, PathAbsent) and isinstance(result, StatResult):
        return _absent(result)
    if isinstance(check, FilePermissions | FileOwner | FileGroup) and isinstance(
        result, StatResult
    ):
        return _attribute(check, result)
    if isinstance(check, FileContent) and isinstance(result, ReadResult):
        return _content(check, result, pattern)
    if isinstance(check, ProcessRunning | ProcessNotRunning) and isinstance(
        result, ProcessesResult
    ):
        return _processes(check, result, pattern)
    return _ERROR


def _leaf_message(check: BaseModel, status: Status, reason: Reason | None) -> str:
    if status == Status.ERROR:
        return ERROR_MESSAGE
    if status == Status.PASSED:
        return getattr(check, "pass_message", None) or PASSED_MESSAGE
    if reason == Reason.NO_ANSWER:
        return NO_ANSWER_MESSAGE
    return getattr(check, "fail_message", None) or FAILED_MESSAGE


def combine(kind: str, statuses: Sequence[Status]) -> Status:
    if kind == "not":
        (status,) = statuses
        return {Status.PASSED: Status.FAILED, Status.FAILED: Status.PASSED}.get(status, status)
    if kind == "all":
        if Status.FAILED in statuses:
            return Status.FAILED
        return Status.ERROR if Status.ERROR in statuses else Status.PASSED
    if Status.PASSED in statuses:
        return Status.PASSED
    return Status.ERROR if Status.ERROR in statuses else Status.FAILED


def _operator_message(node: AllOf | AnyOf | NotOf, status: Status) -> str | None:
    if status == Status.FAILED:
        return node.fail_message
    if status == Status.ERROR:
        return ERROR_MESSAGE
    return None


def _evaluate(node: BaseModel, plan: Plan, facts: Facts | None, answer: str | None) -> Node:
    if isinstance(node, AllOf | AnyOf | NotOf):
        if isinstance(node, AllOf):
            kind, members = "all", node.all
        elif isinstance(node, AnyOf):
            kind, members = "any", node.any
        else:
            kind, members = "not", [node.not_]
        children = tuple(_evaluate(child, plan, facts, answer) for child in members)
        status = combine(kind, [child.status for child in children])
        return Node(
            kind=kind,
            status=status,
            message=_operator_message(node, status),
            children=children,
        )
    leaf = node.model_dump(include={"id", "type"})
    status, reason = evaluate_check(node, facts, plan.patterns.get(leaf["id"]), answer)
    return Node(
        kind="check",
        status=status,
        id=leaf["id"],
        type=leaf["type"],
        reason=reason,
        message=_leaf_message(node, status, reason),
    )


def evaluate(plan: Plan, facts: Facts | None, answer: str | None) -> Node:
    """The result of the whole tree. `facts` is None when the probe gave no answer
    (every condition that needs a fact is then `error`)."""
    return _evaluate(plan.tree, plan, facts, answer)


def to_record(node: Node) -> dict[str, object]:
    """What validation_runs keeps: statuses and closed reasons, nothing else."""
    if node.kind == "check":
        return {
            "kind": "check",
            "id": node.id,
            "type": node.type,
            "status": node.status.value,
            "reason": node.reason.value if node.reason else None,
        }
    return {
        "kind": node.kind,
        "status": node.status.value,
        "children": [to_record(child) for child in node.children],
    }
