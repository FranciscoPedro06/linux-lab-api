"""Evaluating condition trees against facts: leaf outcomes, operators and messages.

Pure functions: facts are built by the stand-in probe in fake_probe.py.
"""

import json
from itertools import product
from typing import Any

import pytest

from linuxlab.validation.evaluate import (
    ERROR_MESSAGE,
    FAILED_MESSAGE,
    NO_ANSWER_MESSAGE,
    PASSED_MESSAGE,
    Node,
    Reason,
    Status,
    combine,
    evaluate,
    normalize,
    to_record,
)
from linuxlab.validation.probe import Facts, parse_answer, request_body

from .fake_probe import Entry, LabState, Process
from .support import PARAMS, leaf, make_plan

HOME = "/home/student"


def run(
    validation: dict[str, Any],
    state: LabState | None = None,
    answer: str | None = None,
    *,
    probe_fails: bool = False,
) -> Node:
    plan = make_plan(validation)
    facts: Facts | None = None
    if plan.facts and not probe_fails:
        request = json.loads(request_body(plan.facts))
        facts = parse_answer(plan.facts, (state or LabState()).answer(request))
    return evaluate(plan, facts, answer)


def outcome(node: Node) -> tuple[Status, Reason | None]:
    return node.status, node.reason


PASSED = (Status.PASSED, None)


def failed(reason: Reason) -> tuple[Status, Reason]:
    return Status.FAILED, reason


# Files


def test_file_exists() -> None:
    check = leaf("c", "file_exists", path=f"{HOME}/a")
    assert outcome(run(check, LabState({f"{HOME}/a": Entry()}))) == PASSED
    assert outcome(run(check)) == failed(Reason.NOT_FOUND)
    assert outcome(run(check, LabState({f"{HOME}/a": Entry(type="directory")}))) == failed(
        Reason.WRONG_TYPE
    )
    assert outcome(run(check, LabState({f"{HOME}/a": Entry(type="symlink")}))) == failed(
        Reason.WRONG_TYPE
    )
    assert outcome(run(check, LabState({f"{HOME}/a": Entry(type="fifo")}))) == failed(
        Reason.WRONG_TYPE
    )
    linked = LabState({f"{HOME}/a": Entry()}, linked={f"{HOME}/a"})
    assert outcome(run(check, linked)) == failed(Reason.WRONG_TYPE)


def test_directory_exists() -> None:
    check = leaf("c", "directory_exists", path=f"{HOME}/d")
    assert outcome(run(check, LabState({f"{HOME}/d": Entry(type="directory")}))) == PASSED
    assert outcome(run(check, LabState({f"{HOME}/d": Entry()}))) == failed(Reason.WRONG_TYPE)
    assert outcome(run(check)) == failed(Reason.NOT_FOUND)


def test_path_absent() -> None:
    check = leaf("c", "path_absent", path=f"{HOME}/x")
    assert outcome(run(check)) == PASSED
    assert outcome(run(check, LabState({f"{HOME}/x": Entry()}))) == failed(Reason.MISMATCH)
    assert outcome(run(check, LabState({f"{HOME}/x": Entry(type="symlink")}))) == failed(
        Reason.MISMATCH
    )
    assert outcome(run(check, LabState(linked={f"{HOME}/x"}))) == failed(Reason.WRONG_TYPE)


def test_file_permissions_owner_and_group() -> None:
    state = LabState({f"{HOME}/a": Entry(mode=0o600, user="student", group="root")})
    mode = leaf("m", "file_permissions", path=f"{HOME}/a", mode="0600")
    assert outcome(run(mode, state)) == PASSED
    assert outcome(run({**mode, "mode": "0644"}, state)) == failed(Reason.MISMATCH)
    owner = leaf("o", "file_owner", path=f"{HOME}/a", user="student")
    assert outcome(run(owner, state)) == PASSED
    assert outcome(run({**owner, "user": "root"}, state)) == failed(Reason.MISMATCH)
    group = leaf("g", "file_group", path=f"{HOME}/a", group="root")
    assert outcome(run(group, state)) == PASSED
    assert outcome(run(mode)) == failed(Reason.NOT_FOUND)
    # A directory's mode counts; a symbolic link's never does.
    directory = LabState({f"{HOME}/a": Entry(type="directory", mode=0o600)})
    assert outcome(run(mode, directory)) == PASSED
    link = LabState({f"{HOME}/a": Entry(type="symlink", mode=0o600)})
    assert outcome(run(mode, link)) == failed(Reason.WRONG_TYPE)


# Content


def content(text: bytes) -> LabState:
    return LabState({f"{HOME}/f": Entry(content=text)})


def test_file_content_comparisons() -> None:
    base = leaf("c", "file_content", path=f"{HOME}/f")
    state = content(b"  DEPLOY_TOKEN=3fa9c01e\n")
    assert outcome(run({**base, "contains": "DEPLOY_TOKEN={{ token }}"}, state)) == PASSED
    assert outcome(run({**base, "contains": "OTHER"}, state)) == failed(Reason.MISMATCH)
    assert outcome(run({**base, "not_contains": "OTHER"}, state)) == PASSED
    assert outcome(run({**base, "not_contains": "{{ token }}"}, state)) == failed(Reason.MISMATCH)
    assert outcome(run({**base, "equals": "DEPLOY_TOKEN=3fa9c01e"}, state)) == PASSED
    assert outcome(run({**base, "regex": "^DEPLOY_TOKEN=[0-9a-f]{8}$"}, state)) == PASSED
    assert outcome(run({**base, "regex": "^DEPLOY_TOKEN={{ token }}$"}, state)) == PASSED
    assert outcome(run({**base, "regex": "^OTHER"}, state)) == failed(Reason.MISMATCH)


def test_trim_applies_to_the_observed_content_only() -> None:
    base = leaf("c", "file_content", path=f"{HOME}/f")
    state = content(b"\n  hello \n")
    assert outcome(run({**base, "equals": "hello"}, state)) == PASSED
    assert outcome(run({**base, "equals": "hello", "trim": False}, state)) == failed(
        Reason.MISMATCH
    )
    assert outcome(run({**base, "equals": "\n  hello \n", "trim": False}, state)) == PASSED
    # The expected value is used as written: trimming the file cannot make it match.
    assert outcome(run({**base, "equals": " hello"}, state)) == failed(Reason.MISMATCH)
    assert outcome(run({**base, "equals": ""}, content(b" \n"))) == PASSED


def test_file_content_states_caused_by_the_student_fail() -> None:
    check = leaf("c", "file_content", path=f"{HOME}/f", contains="x")
    assert outcome(run(check)) == failed(Reason.NOT_FOUND)
    assert outcome(run(check, content(b"x" * (64 * 1024 + 1)))) == failed(Reason.TOO_LARGE)
    assert outcome(run(check, content(b"x" * (64 * 1024)))) == PASSED
    assert outcome(run(check, content(b"x\xff\xfe"))) == failed(Reason.NOT_UTF8)
    for kind in ("fifo", "symlink", "directory", "char_device"):
        state = LabState({f"{HOME}/f": Entry(type=kind)})
        assert outcome(run(check, state)) == failed(Reason.WRONG_TYPE), kind
    assert outcome(run(check, LabState(linked={f"{HOME}/f"}))) == failed(Reason.WRONG_TYPE)


# Processes


def test_process_running_and_not_running() -> None:
    state = LabState(
        processes=[
            Process("sleep", ["sleep", "300"]),
            Process("sleep", ["sleep", "600"], user="root"),
            Process("python3", ["python3", "server.py", "--port", "8080"]),
        ]
    )
    running = leaf("r", "process_running", match={"comm": "sleep"})
    assert outcome(run(running, state)) == PASSED
    assert outcome(run({**running, "min_count": 2}, state)) == PASSED
    assert outcome(run({**running, "min_count": 3}, state)) == failed(Reason.MISMATCH)
    assert outcome(run({**running, "user": "student", "min_count": 2}, state)) == failed(
        Reason.MISMATCH
    )
    by_args = leaf("a", "process_running", match={"args_regex": r"server\.py --port \d+"})
    assert outcome(run(by_args, state)) == PASSED
    assert outcome(run(by_args, LabState())) == failed(Reason.MISMATCH)
    absent = leaf("n", "process_not_running", match={"comm": "python3"})
    assert outcome(run(absent, state)) == failed(Reason.MISMATCH)
    assert outcome(run(absent, LabState())) == PASSED
    assert outcome(run({**absent, "user": "root"}, state)) == PASSED


# Answers


def test_answer() -> None:
    check = leaf("w", "answer", equals_param="word", normalize=["trim", "lowercase"])
    assert outcome(run(check, answer="  LanTern \n")) == PASSED
    assert outcome(run(check, answer="lamp")) == failed(Reason.MISMATCH)
    assert outcome(run(check)) == failed(Reason.NO_ANSWER)
    assert outcome(run(check, answer="")) == failed(Reason.MISMATCH)
    exact = leaf("e", "answer", equals="Lantern")
    assert outcome(run(exact, answer="Lantern")) == PASSED
    assert outcome(run(exact, answer="lantern")) == failed(Reason.MISMATCH)
    assert outcome(run(exact, answer=" Lantern")) == failed(Reason.MISMATCH)
    # Normalization applies to the expected values too.
    one_of = leaf("o", "answer", one_of=["Alpha ", "BETA"], normalize=["lowercase", "trim"])
    assert outcome(run(one_of, answer="beta")) == PASSED
    assert outcome(run(one_of, answer=" alpha")) == PASSED
    assert outcome(run(one_of, answer="gamma")) == failed(Reason.MISMATCH)


def test_normalize_trims_before_lowercasing() -> None:
    assert normalize("  ÀB ", ["lowercase", "trim"]) == "àb"
    assert normalize("  AB ", []) == "  AB "


# Operators


S = (Status.PASSED, Status.FAILED, Status.ERROR)


@pytest.mark.parametrize("statuses", [list(p) for n in (1, 2, 3) for p in product(S, repeat=n)])
def test_operator_tables(statuses: list[Status]) -> None:
    if Status.FAILED in statuses:
        expected_all = Status.FAILED
    elif Status.ERROR in statuses:
        expected_all = Status.ERROR
    else:
        expected_all = Status.PASSED
    if Status.PASSED in statuses:
        expected_any = Status.PASSED
    elif Status.ERROR in statuses:
        expected_any = Status.ERROR
    else:
        expected_any = Status.FAILED
    assert combine("all", statuses) == expected_all
    assert combine("any", statuses) == expected_any


def test_not_swaps_passed_and_failed_and_keeps_error() -> None:
    assert combine("not", [Status.PASSED]) == Status.FAILED
    assert combine("not", [Status.FAILED]) == Status.PASSED
    assert combine("not", [Status.ERROR]) == Status.ERROR


def test_every_leaf_is_evaluated_without_short_circuit() -> None:
    tree = {
        "any": [
            leaf("first", "file_exists", path=f"{HOME}/a"),
            leaf("second", "file_exists", path=f"{HOME}/b"),
        ]
    }
    node = run(tree, LabState({f"{HOME}/a": Entry()}))
    assert node.status == Status.PASSED
    assert [(child.id, child.status) for child in node.children] == [
        ("first", Status.PASSED),
        ("second", Status.FAILED),
    ]
    tree = {"all": [leaf("x", "file_exists", path=f"{HOME}/x"), leaf("y", "answer", equals="a")]}
    node = run(tree, answer="a")
    assert [child.status for child in node.children] == [Status.FAILED, Status.PASSED]


def test_a_probe_without_answer_makes_fact_checks_errors() -> None:
    tree = {
        "all": [
            leaf("file", "file_exists", path=f"{HOME}/a"),
            leaf("word", "answer", equals="a"),
        ]
    }
    node = run(tree, answer="a", probe_fails=True)
    assert [child.status for child in node.children] == [Status.ERROR, Status.PASSED]
    assert node.status == Status.ERROR
    # An infrastructure error never turns into a pass or a failure by itself...
    assert run(leaf("f", "file_exists", path="/a"), probe_fails=True).status == Status.ERROR
    negated = {"not": leaf("f", "file_exists", path="/a"), "fail_message": "Existe."}
    assert run(negated, probe_fails=True).status == Status.ERROR
    # ...but a sibling that genuinely failed still decides `all`, as documented.
    node = run(tree, answer="b", probe_fails=True)
    assert node.status == Status.FAILED


def test_fact_errors_reported_by_the_probe_are_errors() -> None:
    plan = make_plan(leaf("f", "file_exists", path=f"{HOME}/a"))
    output = json.dumps(
        {
            "v": 1,
            "facts": [
                {
                    "op": "stat",
                    "path": f"{HOME}/a",
                    "follow": False,
                    "status": "error",
                    "errno": "EIO",
                }
            ],
        }
    ).encode()
    node = evaluate(plan, parse_answer(plan.facts, output), None)
    assert node.status == Status.ERROR
    assert node.message == ERROR_MESSAGE


# Messages and records


def test_messages_come_from_the_mission_or_defaults() -> None:
    tree = {
        "all": [
            leaf(
                "a", "file_exists", path=f"{HOME}/a", pass_message="Existe.", fail_message="Falta."
            ),
            leaf("b", "file_exists", path=f"{HOME}/b"),
            leaf("c", "answer", equals="x", fail_message="Resposta errada."),
        ],
        "fail_message": "Ainda não.",
    }
    node = run(tree, LabState({f"{HOME}/a": Entry()}))
    assert node.message == "Ainda não."
    assert [child.message for child in node.children] == [
        "Existe.",
        FAILED_MESSAGE,
        NO_ANSWER_MESSAGE,
    ]
    passed = run(leaf("b", "file_exists", path=f"{HOME}/b"), LabState({f"{HOME}/b": Entry()}))
    assert passed.message == PASSED_MESSAGE
    assert run({"all": [leaf("b", "path_absent", path="/b")]}).message is None


def test_results_never_carry_observed_or_expected_values() -> None:
    secret = "DEPLOY_TOKEN=3fa9c01e"
    tree = {
        "all": [
            leaf("c", "file_content", path=f"{HOME}/f", contains="DEPLOY_TOKEN={{ token }}"),
            leaf("w", "answer", equals_param="word"),
            leaf("p", "process_running", match={"args_regex": "--key {{ token }}"}),
        ]
    }
    state = LabState(
        {f"{HOME}/f": Entry(content=b"student wrote this " + secret.encode())},
        processes=[Process("app", ["app", "--key", "student-arg"])],
    )
    node = run(tree, state, answer="my literal answer")
    text = json.dumps(to_record(node)) + repr(node)
    for value in (*PARAMS.values(), "student wrote this", "my literal answer", "student-arg"):
        assert value not in text
    assert to_record(node) == {
        "kind": "all",
        "status": "failed",
        "children": [
            {
                "kind": "check",
                "id": "c",
                "type": "file_content",
                "status": "passed",
                "reason": None,
            },
            {
                "kind": "check",
                "id": "w",
                "type": "answer",
                "status": "failed",
                "reason": "mismatch",
            },
            {
                "kind": "check",
                "id": "p",
                "type": "process_running",
                "status": "failed",
                "reason": "mismatch",
            },
        ],
    }
