"""Question file schema and modules of questions, on parsed YAML. See docs/questions.md."""

from typing import Any

import pytest
from pydantic import ValidationError

from linuxlab.content.schema import MAX_OPTIONS, ModuleFile, QuestionFile
from linuxlab.content.stored import InvalidSpecError, question_spec

from .support import FIXTURE, read_yaml


def question_data(slug: str = "sample-choice") -> dict[str, Any]:
    return read_yaml(FIXTURE / "questions" / f"{slug}.yaml")


def parse(**changes: Any) -> QuestionFile:
    data = question_data()
    data.update(changes)
    return QuestionFile.model_validate(data)


def errors(**changes: Any) -> str:
    with pytest.raises(ValidationError) as error:
        parse(**changes)
    return str(error.value)


def options(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return list(items)


def wrong(id_: str, text: str | None = None) -> dict[str, Any]:
    return {"id": id_, "text": text or f"Alternativa {id_}", "feedback": f"Não é {id_}."}


def right(id_: str) -> dict[str, Any]:
    return {"id": id_, "text": f"Alternativa {id_}"}


def test_fixture_questions_are_valid() -> None:
    question = parse()
    assert question.type == "single_choice"
    assert question.answer == "pwd"
    assert [option.id for option in question.options] == ["cd", "pwd", "ls"]
    assert question.options[1].feedback is None
    for slug in ("sample-path", "sample-choice-draft"):
        QuestionFile.model_validate(question_data(slug))


def test_answer_must_name_an_option() -> None:
    assert "answer: no option has the id mv" in errors(answer="mv")


def test_option_ids_and_texts_are_unique() -> None:
    assert "an option id is used more than once" in errors(
        options=options(right("pwd"), wrong("cd"), wrong("cd", "Outra"))
    )
    assert "two options have the same text" in errors(
        options=options(right("pwd"), wrong("cd", "Igual"), wrong("ls", " Igual "))
    )


def test_every_wrong_option_needs_a_feedback() -> None:
    message = errors(options=options(right("pwd"), {"id": "cd", "text": "`cd`"}))
    assert "the wrong option cd needs a feedback" in message
    blank = errors(options=options(right("pwd"), {**wrong("cd"), "feedback": "  "}))
    assert "must not be blank" in blank


def test_the_correct_option_has_no_feedback() -> None:
    message = errors(options=options({**right("pwd"), "feedback": "Certo."}, wrong("cd")))
    assert "the correct option pwd has no feedback" in message


@pytest.mark.parametrize("field", ["question", "explanation", "title", "summary", "answer"])
def test_required_fields(field: str) -> None:
    data = question_data()
    del data[field]
    with pytest.raises(ValidationError) as error:
        QuestionFile.model_validate(data)
    assert [item["loc"] for item in error.value.errors()] == [(field,)]


@pytest.mark.parametrize("field", ["question", "explanation", "title", "summary"])
def test_texts_are_not_blank(field: str) -> None:
    assert "must not be blank" in errors(**{field: " \n"})


def test_number_of_options() -> None:
    assert "at least 2" in errors(options=options(right("pwd")))
    many = [right("pwd"), *(wrong(f"o{index}") for index in range(MAX_OPTIONS))]
    assert f"at most {MAX_OPTIONS}" in errors(options=many)
    parse(options=[right("pwd"), *(wrong(f"o{index}") for index in range(MAX_OPTIONS - 1))])


@pytest.mark.parametrize("option_id", ["", "A", "a b", "a_b", "x" * 33, 1])
def test_option_ids_are_short_slugs(option_id: Any) -> None:
    with pytest.raises(ValidationError):
        parse(options=options(right("pwd"), {**wrong("cd"), "id": option_id}))


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "multiple_choice"},
        {"type": "short_answer"},
        {"unknown": 1},
        {"params": {}},
        {"difficulty": "1"},
        {"difficulty": 6},
        {"estimated_minutes": 0},
        {"status": "hidden"},
        {"tags": ["text", "text"]},
        {"options": "a, b"},
        {"hints": "uma dica"},
        {"slug": "Sample"},
        {"schema": 2},
    ],
)
def test_wrong_types_and_unknown_fields_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse(**changes)


def test_unknown_option_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        parse(options=options(right("pwd"), {**wrong("cd"), "correct": True}))


# Modules


def module_data(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": 1,
        "slug": "first-steps",
        "title": "Primeiros passos",
        "description": "Descrição.",
        "status": "draft",
    }
    data.update(changes)
    return data


def test_a_module_lists_missions_or_questions() -> None:
    assert ModuleFile.model_validate(module_data(questions=["a", "b"])).questions == ["a", "b"]
    assert ModuleFile.model_validate(module_data(missions=["a"])).missions == ["a"]
    assert ModuleFile.model_validate(module_data(questions=[])).questions == []


@pytest.mark.parametrize(
    "changes",
    [
        {"missions": ["a"], "questions": ["b"]},
        {"missions": [], "questions": []},
        {},
        {"questions": ["a", "a"]},
        {"questions": ["A"]},
        {"questions": None},
    ],
)
def test_mixed_and_malformed_modules_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModuleFile.model_validate(module_data(**changes))


def test_mixed_module_message() -> None:
    with pytest.raises(ValidationError) as error:
        ModuleFile.model_validate(module_data(missions=["a"], questions=["b"]))
    assert "exactly one of missions, questions is required" in str(error.value)


# A stored version, as the answer endpoint reads it


def stored(slug: str = "sample-choice") -> dict[str, Any]:
    data = question_data(slug)
    del data["status"]
    return data


def test_a_stored_version_reads_back() -> None:
    question = question_spec(stored())
    assert (question.answer, question.explanation) == (
        "pwd",
        "Explicação sintética da questão de teste.\n",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"answer": "mv"},
        {"options": [right("pwd")]},
        {"status": "published"},
        {"type": "short_answer"},
        {"extra": 1},
    ],
)
def test_a_stored_version_that_breaks_the_rules_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(InvalidSpecError):
        question_spec({**stored(), **change})


@pytest.mark.parametrize("value", [None, [], "spec", 1])
def test_a_stored_version_that_is_not_an_object_is_refused(value: Any) -> None:
    with pytest.raises(InvalidSpecError):
        question_spec(value)
