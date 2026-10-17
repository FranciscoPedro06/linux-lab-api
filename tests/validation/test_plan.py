"""Reading a stored condition tree and resolving it with a lab's parameters."""

from typing import Any

import pytest

from linuxlab.content.schema import FileContent, ProcessRunning, checks
from linuxlab.content.stored import InvalidSpecError, validation_spec
from linuxlab.validation.plan import plan
from linuxlab.validation.probe import ProcessesFact, ReadFact, StatFact

from .support import PARAMS, leaf, make_plan, stored


def test_facts_are_deduplicated_in_tree_order() -> None:
    result = make_plan(
        {
            "all": [
                leaf("a", "file_exists", path="/home/student/a"),
                leaf("b", "file_permissions", path="/home/student/a", mode="0600"),
                leaf("c", "file_content", path="/home/student/a", contains="x"),
                leaf("d", "file_content", path="/home/student/a", equals="y"),
                leaf("e", "process_running", match={"comm": "sleep"}),
                leaf("f", "process_not_running", match={"comm": "yes"}),
                leaf("g", "directory_exists", path="/home/student/a", follow_symlinks=True),
                leaf("h", "path_absent", path="/home/student/b"),
            ]
        }
    )
    assert result.facts == (
        StatFact("/home/student/a", follow=False),
        ReadFact("/home/student/a"),
        ProcessesFact(),
        StatFact("/home/student/a", follow=True),
        StatFact("/home/student/b", follow=False),
    )


def test_answer_only_trees_need_no_probe() -> None:
    assert make_plan(leaf("w", "answer", equals_param="word")).facts == ()


def test_placeholders_are_replaced_in_memory() -> None:
    tree: dict[str, Any] = {
        "all": [
            leaf(
                "c", "file_content", path="/home/student/{{ token }}.txt", contains="T={{ token }}"
            ),
            leaf("r", "file_content", path="/home/student/r", regex="^T={{ token }}$"),
            leaf("p", "process_running", match={"args_regex": "sleep {{ token }}"}),
        ]
    }
    result = make_plan(tree)
    content, regex, process = list(checks(result.tree))
    assert isinstance(content, FileContent) and isinstance(regex, FileContent)
    assert content.path == "/home/student/3fa9c01e.txt"
    assert content.contains == "T=3fa9c01e"
    assert regex.regex == "^T=3fa9c01e$"
    assert isinstance(process, ProcessRunning)
    assert process.match.args_regex == "sleep 3fa9c01e"
    assert result.patterns["r"].search("T=3fa9c01e") is not None
    # The stored tree keeps its placeholders.
    original = next(checks(validation_spec(stored(tree)).tree))
    assert isinstance(original, FileContent) and original.path == "/home/student/{{ token }}.txt"


def test_parameter_values_are_escaped_in_patterns() -> None:
    result = make_plan(
        leaf("r", "file_content", path="/home/student/r", regex="^[{{ token }}]+$"),
        params={"token": "a-c", "word": "x"},
    )
    pattern = result.patterns["r"]
    assert pattern.search("b") is None
    assert pattern.search("a-c") is not None


def test_equals_param_becomes_the_labs_value() -> None:
    result = make_plan(leaf("w", "answer", equals_param="word"))
    (answer,) = list(checks(result.tree))
    assert answer.model_dump(include={"equals", "equals_param"}) == {
        "equals": "lantern",
        "equals_param": None,
    }


def test_a_pattern_longer_than_the_limit_after_replacement_still_compiles() -> None:
    pattern = "x" * 1010 + "{{ token }}"
    result = make_plan(
        leaf("r", "file_content", path="/r", regex=pattern), params={"token": "f" * 64, "word": "x"}
    )
    assert result.patterns["r"].search("x" * 1010 + "f" * 64) is not None


@pytest.mark.parametrize(
    "params",
    [
        {},  # the lab lacks a parameter the tree uses
        {"token": "UPPER", "word": "x"},  # outside the parameter format
        {"token": "a/b", "word": "x"},
        {"token": "", "word": "x"},
    ],
)
def test_unusable_parameters_are_an_invalid_spec(params: dict[str, str]) -> None:
    with pytest.raises(InvalidSpecError):
        make_plan(leaf("c", "file_content", path="/home/student/{{ token }}", contains="x"), params)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"validation": {"id": "a", "type": "run_command", "command": "id"}, "params": {}},
        {"validation": {"id": "a", "type": "file_exists", "path": "/a/../b"}, "params": {}},
        {"validation": leaf("r", "file_content", path="/r", regex="(?=x)"), "params": {}},
        {"validation": leaf("c", "file_content", path="/{{ nope }}", contains="x"), "params": {}},
        {
            "validation": leaf("c", "file_exists", path="/x", fail_message="{{ token }}"),
            "params": {},
        },
        {
            "validation": leaf("c", "file_exists", path="/x"),
            "params": {"token": {"generator": "x"}},
        },
        {"validation": "x", "params": {}},
    ],
    ids=["empty", "unknown-type", "dotdot", "re2", "unknown-param", "message", "param", "text"],
)
def test_invalid_stored_trees_are_refused(spec: dict[str, Any]) -> None:
    with pytest.raises(InvalidSpecError) as error:
        validation_spec(spec)
    assert "{{" not in str(error.value)


def test_plan_reads_the_parameters_it_is_given() -> None:
    tree = validation_spec(stored(leaf("c", "path_absent", path="/home/student/{{ word }}"))).tree
    assert plan(tree, PARAMS).facts == (StatFact("/home/student/lantern", follow=False),)
