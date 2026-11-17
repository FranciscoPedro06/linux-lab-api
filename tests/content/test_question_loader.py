"""Reading content/questions/ and modules of questions. See docs/questions.md."""

import shutil
from pathlib import Path

import pytest

from linuxlab.content.loader import (
    QUESTION_FILE_MAX_BYTES,
    ContentError,
    ContentInvalid,
    content_hash,
    load_content,
)

from .support import FIXTURE, copy_fixture, edit_yaml, mission_file, module_file


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


def question_file(root: Path, slug: str) -> Path:
    return root / "questions" / f"{slug}.yaml"


def messages(root: Path) -> list[str]:
    with pytest.raises(ContentInvalid) as error:
        load_content(root)
    errors: list[ContentError] = error.value.errors
    return [str(item) for item in errors]


def test_fixture_questions_are_placed_in_their_module() -> None:
    content = load_content(FIXTURE)

    quiz = next(module for module in content.modules if module.slug == "quiz")
    assert quiz.missions == ()
    assert quiz.questions == ("sample-choice", "sample-path", "sample-choice-draft")
    alpha = next(module for module in content.modules if module.slug == "alpha")
    assert alpha.questions == ()
    placed = [(q.module, q.position, q.slug, q.status) for q in content.questions]
    assert placed == [
        ("quiz", 1, "sample-choice", "published"),
        ("quiz", 2, "sample-path", "published"),
        ("quiz", 3, "sample-choice-draft", "draft"),
    ]
    assert not content.empty


def test_spec_holds_every_field_but_the_status() -> None:
    question = load_content(FIXTURE).questions[0]
    spec = question.spec

    assert "status" not in spec
    assert spec["schema"] == 1
    assert spec["type"] == "single_choice"
    assert spec["answer"] == "pwd"
    # The correct option has no feedback key at all.
    assert spec["options"][1] == {"id": "pwd", "text": "`pwd`"}
    assert spec["options"][0]["feedback"] == "Feedback sintético da alternativa cd."
    assert question.content_hash == content_hash(spec)


def test_status_and_position_do_not_change_the_hash(root: Path) -> None:
    before = {q.slug: q.content_hash for q in load_content(root).questions}
    edit_yaml(question_file(root, "sample-choice-draft"), lambda d: d.update(status="published"))
    edit_yaml(
        module_file(root, "quiz"),
        lambda d: d.update(questions=["sample-path", "sample-choice", "sample-choice-draft"]),
    )
    after = {q.slug: q.content_hash for q in load_content(root).questions}
    assert after == before

    edit_yaml(question_file(root, "sample-path"), lambda d: d.update(explanation="Outra."))
    changed = {q.slug: q.content_hash for q in load_content(root).questions}
    assert changed["sample-path"] != before["sample-path"]
    assert changed["sample-choice"] == before["sample-choice"]


def test_a_module_with_missions_and_questions_is_refused(root: Path) -> None:
    edit_yaml(module_file(root, "alpha"), lambda d: d.update(questions=["sample-choice"]))
    found = messages(root)
    assert "modules/alpha.yaml: exactly one of missions, questions is required" in found


def test_question_not_listed_in_any_module(root: Path) -> None:
    edit_yaml(module_file(root, "quiz"), lambda d: d["questions"].remove("sample-path"))
    assert messages(root) == ["questions/sample-path.yaml: not listed in any module"]


def test_question_in_two_modules(root: Path, tmp_path: Path) -> None:
    edit_yaml(
        module_file(root, "quiz"),
        lambda d: d.update(questions=["sample-choice", "sample-choice-draft"]),
    )
    (root / "modules" / "zeta.yaml").write_text(
        "schema: 1\nslug: zeta\ntitle: Z\ndescription: Z.\nstatus: draft\n"
        "questions: [sample-path, sample-choice]\n",
        encoding="utf-8",
    )
    assert messages(root) == [
        "modules/zeta.yaml: questions.1: question sample-choice is already in module quiz"
    ]


def test_module_refers_to_a_missing_question(root: Path) -> None:
    edit_yaml(module_file(root, "quiz"), lambda d: d["questions"].append("sample-missing"))
    assert messages(root) == [
        "modules/quiz.yaml: questions.3: no question file questions/sample-missing.yaml"
    ]


def test_a_question_listed_as_a_mission_or_a_mission_as_a_question(root: Path) -> None:
    edit_yaml(module_file(root, "beta"), lambda d: d["missions"].append("sample-choice"))
    edit_yaml(module_file(root, "quiz"), lambda d: d["questions"].append("sample-draft"))
    found = messages(root)
    assert "modules/beta.yaml: missions.1: no mission directory missions/sample-choice" in found
    assert "modules/quiz.yaml: questions.3: no question file questions/sample-draft.yaml" in found


def test_question_files_problems(root: Path) -> None:
    (root / "questions" / "notes.txt").write_text("x", encoding="utf-8")
    (root / "questions" / "nested").mkdir()
    (root / "questions" / "Bad_Name.yaml").write_text("schema: 1\n", encoding="utf-8")

    found = messages(root)
    assert "questions/notes.txt: questions/ holds only <slug>.yaml files" in found
    assert "questions/nested: questions/ holds only <slug>.yaml files" in found
    assert (
        "questions/Bad_Name.yaml: file name must be a slug ([a-z0-9-], up to 64) and .yaml" in found
    )


def test_slug_must_match_the_file_name(root: Path) -> None:
    edit_yaml(question_file(root, "sample-path"), lambda d: d.update(slug="sample-other"))
    assert messages(root) == ["questions/sample-path.yaml: slug: must match the file name"]


def test_schema_errors_name_the_file_and_field(root: Path) -> None:
    edit_yaml(question_file(root, "sample-path"), lambda d: d.update(answer="missing"))
    edit_yaml(question_file(root, "sample-choice"), lambda d: d.update(difficulty=9))
    found = messages(root)
    assert "questions/sample-path.yaml: answer: no option has the id missing" in found
    assert any(
        message.startswith("questions/sample-choice.yaml: difficulty: ") for message in found
    )


def test_question_file_size_limit(root: Path) -> None:
    edit_yaml(
        question_file(root, "sample-path"),
        lambda d: d.update(explanation="x" * QUESTION_FILE_MAX_BYTES),
    )
    assert messages(root) == ["questions/sample-path.yaml: file is larger than 64 KiB"]


def test_yaml_rules_apply_to_questions(root: Path) -> None:
    question_file(root, "sample-path").write_text(
        "schema: 1\nslug: sample-path\nslug: sample-path\n", encoding="utf-8"
    )
    (found,) = messages(root)
    assert found.startswith("questions/sample-path.yaml: invalid YAML: ")
    assert "duplicate key 'slug'" in found


def test_content_with_questions_only_is_not_empty(root: Path) -> None:
    for slug in ("alpha", "beta"):
        module_file(root, slug).unlink()
    shutil.rmtree(root / "missions")
    content = load_content(root)
    assert content.missions == ()
    assert len(content.questions) == 3
    assert not content.empty


def test_missions_are_unaffected_by_questions(root: Path) -> None:
    with_questions = load_content(root)
    for path in (root / "questions").iterdir():
        path.unlink()
    module_file(root, "quiz").unlink()
    without = load_content(root)
    assert without.missions == with_questions.missions
    assert mission_file(root, "sample-file").exists()
