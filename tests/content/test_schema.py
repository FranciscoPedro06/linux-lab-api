"""Mission and module file schema, on parsed YAML."""

from typing import Any

import pytest
from pydantic import ValidationError

from linuxlab.content.schema import MAX_CONDITION_DEPTH, MAX_CONDITIONS, MissionFile, ModuleFile

from .support import mission_data


def check(id_: str, **fields: Any) -> dict[str, Any]:
    return {"id": id_, "type": "file_exists", "path": "/home/student/x", **fields}


def parse(**changes: Any) -> MissionFile:
    data = mission_data()
    data.update(changes)
    return MissionFile.model_validate(data)


def errors(**changes: Any) -> str:
    with pytest.raises(ValidationError) as error:
        parse(**changes)
    return str(error.value)


def test_fixture_mission_is_valid() -> None:
    mission = parse()
    assert mission.slug == "sample-file"
    assert not mission.has_answer()
    assert MissionFile.model_validate(mission_data("sample-answer")).has_answer()


def test_unknown_fields_are_rejected() -> None:
    assert "Extra inputs are not permitted" in errors(author="x")
    assert "Extra inputs are not permitted" in errors(
        setup={"script": "setup.sh", "timeout_seconds": 5, "shell": "sh"}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", 2),
        ("slug", "Sample"),
        ("slug", "a" * 65),
        ("status", "public"),
        ("difficulty", 0),
        ("difficulty", 6),
        ("difficulty", True),
        ("difficulty", "1"),
        ("estimated_minutes", 0),
        ("title", "   "),
        ("title", 12),
        ("objectives", []),
        ("tags", ["permissions", "permissions"]),
        ("tags", ["Permissions"]),
        ("hints", [""]),
    ],
)
def test_invalid_values_are_rejected(field: str, value: Any) -> None:
    errors(**{field: value})


def test_required_fields() -> None:
    for field in ("slug", "title", "briefing", "validation", "setup", "environment"):
        data = mission_data()
        del data[field]
        with pytest.raises(ValidationError, match=field):
            MissionFile.model_validate(data)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../other/briefing.md", "a/../b.md", "a\\b.md", "./b.md", ""]
)
def test_file_references_stay_inside_the_mission(path: str) -> None:
    errors(briefing=path)


def test_setup_rules() -> None:
    assert parse(setup={"script": "setup.sh", "timeout_seconds": 60}).setup.user == "student"
    errors(setup={"script": "setup.sh", "timeout_seconds": 61})
    errors(setup={"script": "setup.sh", "timeout_seconds": 5, "user": "admin"})
    errors(environment={"image": "base", "profile": "privileged"})


def test_solutions_and_counterexamples() -> None:
    one = [{"label": "A", "script": "tests/solution-octal.sh"}]
    errors(solutions=one)
    errors(solutions=one * 2)  # the same script twice
    errors(counterexamples=[])


def test_mode_must_be_a_string() -> None:
    # Unquoted in YAML, 700 is an integer and 0700 the octal integer 448.
    for mode in (700, 448):
        errors(validation={"id": "mode", "type": "file_permissions", "path": "/x", "mode": mode})
    errors(validation={"id": "mode", "type": "file_permissions", "path": "/x", "mode": "700"})
    parse(validation={"id": "mode", "type": "file_permissions", "path": "/x", "mode": "0755"})


def test_condition_tree_limits() -> None:
    parse(validation={"all": [check(f"c{i}") for i in range(MAX_CONDITIONS)]})
    message = errors(validation={"all": [check(f"c{i}") for i in range(MAX_CONDITIONS + 1)]})
    assert f"more than {MAX_CONDITIONS} conditions" in message

    node: dict[str, Any] = check("deep")
    for _ in range(MAX_CONDITION_DEPTH - 1):
        node = {"all": [node]}
    parse(validation=node)
    assert "nested deeper" in errors(validation={"all": [node]})


def test_condition_tree_structure() -> None:
    assert "used more than once" in errors(validation={"all": [check("a"), check("a")]})
    assert "exactly one of all, any, not" in errors(
        validation={"all": [check("a")], "any": [check("b")]}
    )
    assert "exactly one of all, any, not" in errors(validation={"id": "a", "path": "/x"})
    errors(validation={"not": check("a")})  # not requires fail_message
    errors(validation={"all": []})
    errors(validation={"id": "a", "type": "run_command", "command": "id"})
    parse(validation={"not": check("a"), "fail_message": "Ainda existe."})


@pytest.mark.parametrize(
    "condition",
    [
        {"type": "file_content", "path": "/x"},
        {"type": "file_content", "path": "/x", "equals": "a", "contains": "b"},
        {"type": "file_exists", "path": "relative/path"},
        {"type": "process_running", "match": {"comm": "sleep", "args_regex": "x"}},
        {"type": "process_running", "match": {}},
        {"type": "answer", "equals": "a", "one_of": ["a"]},
        {"type": "answer", "equals_param": "missing"},
        {"type": "answer", "equals": "a", "normalize": ["trim", "trim"]},
        {"type": "file_owner", "path": "/x", "user": "Root User"},
    ],
)
def test_invalid_conditions(condition: dict[str, Any]) -> None:
    errors(validation={"id": "c", **condition})


def test_placeholders_name_declared_parameters() -> None:
    contains = {"id": "c", "type": "file_content", "path": "/x"}
    parse(validation={**contains, "contains": "TOKEN={{ token }}"})
    assert "unknown parameter" in errors(validation={**contains, "contains": "{{ secret }}"})
    assert "malformed" in errors(validation={**contains, "contains": "{{ token"})
    assert "unknown parameter" in errors(validation={**contains, "contains": "{{ token.upper() }}"})


@pytest.mark.parametrize(
    "params",
    [
        {"token": {"generator": "uuid"}},
        {"token": {"generator": "hex"}},
        {"token": {"generator": "hex", "length": 65}},
        {"token": {"generator": "int", "min": 5, "max": 1}},
        {"token": {"generator": "choice", "values": ["Upper"]}},
        {"token": {"generator": "choice", "values": []}},
        {"Token": {"generator": "word"}},
    ],
)
def test_invalid_parameters(params: dict[str, Any]) -> None:
    errors(params=params, validation=check("c"))


def test_module_file() -> None:
    module = {
        "schema": 1,
        "slug": "alpha",
        "title": "Módulo",
        "description": "Descrição.",
        "status": "published",
        "missions": ["a", "b"],
    }
    assert ModuleFile.model_validate(module).missions == ["a", "b"]
    for change in (
        {"missions": ["a", "a"]},
        {"missions": ["A"]},
        {"position": 1},
        {"status": "hidden"},
        {"title": ""},
    ):
        with pytest.raises(ValidationError):
            ModuleFile.model_validate({**module, **change})
