"""Syncing questions against PostgreSQL: versions, idempotence, archiving, immutability."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from linuxlab.content.loader import load_content
from linuxlab.content.sync import SyncReport, sync_content
from tests.auth.support import run, sql

from .support import copy_fixture, edit_yaml, module_file

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


def sync(client: TestClient, root: Path, *, allow_empty: bool = False) -> SyncReport:
    content = load_content(root)
    return run(
        client,
        lambda: sync_content(client.app.state.sessionmaker, content, allow_empty=allow_empty),  # type: ignore[attr-defined]
    )


def question_file(root: Path, slug: str) -> Path:
    return root / "questions" / f"{slug}.yaml"


def questions(client: TestClient) -> dict[str, tuple[Any, ...]]:
    """slug -> (module slug, position, status, current version)."""
    rows = sql(
        client,
        "SELECT q.slug, mo.slug, q.position, q.status, q.current_version"
        " FROM questions q JOIN modules mo ON mo.id = q.module_id",
    )
    return {row[0]: tuple(row[1:]) for row in rows}


def versions(client: TestClient, slug: str) -> list[tuple[Any, ...]]:
    rows = sql(
        client,
        "SELECT v.version, v.content_hash, v.spec FROM question_versions v"
        " JOIN questions q ON q.id = v.question_id WHERE q.slug = :slug ORDER BY v.version",
        slug=slug,
    )
    return [tuple(row) for row in rows]


def test_first_sync_creates_questions_at_version_1(client: TestClient, root: Path) -> None:
    report = sync(client, root)

    assert (report.questions_created, report.question_versions_created) == (3, 3)
    assert questions(client) == {
        "sample-choice": ("quiz", 1, "published", 1),
        "sample-path": ("quiz", 2, "published", 1),
        "sample-choice-draft": ("quiz", 3, "draft", 1),
    }
    for question in load_content(root).questions:
        ((version, content_hash, spec),) = versions(client, question.slug)
        assert (version, content_hash, spec) == (1, question.content_hash, question.spec)
    # The answer key is stored, for the answer endpoint only.
    assert versions(client, "sample-choice")[0][2]["answer"] == "pwd"


def test_repeated_sync_changes_nothing(client: TestClient, root: Path) -> None:
    sync(client, root)
    before = sorted(sql(client, "SELECT * FROM questions"))
    assert sync(client, root) == SyncReport()
    assert sorted(sql(client, "SELECT * FROM questions")) == before


def test_changed_question_gets_a_new_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(question_file(root, "sample-choice"), lambda d: d.update(answer="cd"))
    edit_yaml(
        question_file(root, "sample-choice"),
        lambda d: d.update(
            options=[
                {"id": "cd", "text": "`cd`"},
                {"id": "pwd", "text": "`pwd`", "feedback": "Agora errada."},
                {"id": "ls", "text": "`ls`", "feedback": "Errada."},
            ]
        ),
    )

    assert sync(client, root) == SyncReport(question_versions_created=1)
    assert questions(client)["sample-choice"] == ("quiz", 1, "published", 2)
    first, second = versions(client, "sample-choice")
    assert (first[2]["answer"], second[2]["answer"]) == ("pwd", "cd")


def test_status_and_position_change_without_a_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(question_file(root, "sample-choice-draft"), lambda d: d.update(status="published"))
    edit_yaml(
        module_file(root, "quiz"),
        lambda d: d.update(questions=["sample-choice-draft", "sample-choice", "sample-path"]),
    )

    assert sync(client, root) == SyncReport(questions_updated=3)
    assert questions(client) == {
        "sample-choice-draft": ("quiz", 1, "published", 1),
        "sample-choice": ("quiz", 2, "published", 1),
        "sample-path": ("quiz", 3, "published", 1),
    }


def test_removed_question_is_archived_with_its_versions(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(module_file(root, "quiz"), lambda d: d["questions"].remove("sample-path"))
    question_file(root, "sample-path").unlink()

    assert sync(client, root) == SyncReport(questions_archived=1)
    assert questions(client)["sample-path"] == ("quiz", None, "archived", 1)
    assert len(versions(client, "sample-path")) == 1


def test_removed_module_of_questions_is_archived(client: TestClient, root: Path) -> None:
    sync(client, root)
    module_file(root, "quiz").unlink()
    for path in (root / "questions").iterdir():
        path.unlink()

    assert sync(client, root) == SyncReport(modules_archived=1, questions_archived=3)
    assert sql(client, "SELECT status FROM modules WHERE slug = 'quiz'") == [("archived",)]


def test_a_module_can_change_from_missions_to_questions(client: TestClient, root: Path) -> None:
    sync(client, root)

    def to_questions(data: dict[str, Any]) -> None:
        del data["missions"]
        data["questions"] = ["sample-choice", "sample-path", "sample-choice-draft"]

    # beta lists the questions now, quiz is gone, and beta's mission moves to alpha.
    edit_yaml(module_file(root, "beta"), to_questions)
    module_file(root, "quiz").unlink()
    edit_yaml(module_file(root, "alpha"), lambda d: d["missions"].append("sample-draft"))

    report = sync(client, root)
    assert report.modules_archived == 1
    assert questions(client)["sample-choice"] == ("beta", 1, "published", 1)


def test_question_versions_are_immutable(client: TestClient, root: Path) -> None:
    sync(client, root)
    with pytest.raises(DBAPIError, match="question versions cannot be changed or deleted"):
        sql(client, "UPDATE question_versions SET content_hash = repeat('0', 64)")
    with pytest.raises(DBAPIError, match="question versions cannot be changed or deleted"):
        sql(client, "DELETE FROM question_versions")


def test_missions_sync_as_before(client: TestClient, root: Path) -> None:
    report = sync(client, root)
    assert (report.missions_created, report.versions_created) == (3, 3)
    assert sql(client, "SELECT count(*) FROM missions WHERE module_id IN"
               " (SELECT id FROM modules WHERE slug = 'quiz')") == [(0,)]  # fmt: skip
