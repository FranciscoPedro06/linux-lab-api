"""Catalog endpoints against PostgreSQL, with synthetic content synced first."""

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from tests.auth.support import request, run, session_token, signup
from tests.content.support import copy_fixture, edit_yaml, mission_file, module_file

pytestmark = pytest.mark.integration

CARD_FIELDS = {
    "slug",
    "title",
    "summary",
    "difficulty",
    "estimated_minutes",
    "tags",
    "version",
    "requires_answer",
}
DETAIL_FIELDS = CARD_FIELDS | {"module", "briefing", "objectives", "hints"}
# Never sent to the client, at any level of the response.
HIDDEN = ("setup", "params", "validation", "solutions", "counterexamples", "explanation")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


@pytest.fixture
def token(client: TestClient) -> str:
    return session_token(signup(client))


def sync(client: TestClient, root: Path, *, allow_empty: bool = False) -> None:
    content = load_content(root)
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, content, allow_empty=allow_empty))


def modules(client: TestClient, token: str | None) -> list[dict[str, Any]]:
    response = request(client, "GET", "/api/modules", token=token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def mission(client: TestClient, token: str | None, slug: str) -> dict[str, Any]:
    response = request(client, "GET", f"/api/missions/{slug}", token=token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def assert_not_found(client: TestClient, token: str, slug: str) -> None:
    response = request(client, "GET", f"/api/missions/{slug}", token=token)
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "mission_not_found", "message": "Missão não encontrada."}
    }


def test_catalog_requires_a_session(client: TestClient, root: Path) -> None:
    sync(client, root)
    for path in ("/api/modules", "/api/missions/sample-file"):
        for token in (None, "unknown-token"):
            response = request(client, "GET", path, token=token)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "not_authenticated"


def test_empty_catalog(client: TestClient, token: str) -> None:
    assert modules(client, token) == []
    assert_not_found(client, token, "sample-file")


def test_list_shows_published_content_in_order(client: TestClient, token: str, root: Path) -> None:
    sync(client, root)

    body = modules(client, token)

    # beta has only a draft mission, so it is not listed.
    assert body == [
        {
            "slug": "alpha",
            "title": "Módulo de teste A",
            "description": "Conteúdo sintético usado pelos testes.",
            "missions": [
                {
                    "slug": "sample-file",
                    "title": "Arquivo de teste",
                    "summary": "Missão sintética sobre permissões de um arquivo.",
                    "difficulty": 1,
                    "estimated_minutes": 5,
                    "tags": ["permissions", "chmod"],
                    "version": 1,
                    "requires_answer": False,
                },
                {
                    "slug": "sample-answer",
                    "title": "Resposta de teste",
                    "summary": "Missão sintética que espera uma resposta.",
                    "difficulty": 2,
                    "estimated_minutes": 10,
                    "tags": [],
                    "version": 1,
                    "requires_answer": True,
                },
            ],
        }
    ]


def test_modules_are_ordered_by_slug(client: TestClient, token: str, root: Path) -> None:
    edit_yaml(mission_file(root, "sample-draft"), lambda d: d.update(status="published"))
    edit_yaml(module_file(root, "beta"), lambda d: d.update(slug="aaa"))
    module_file(root, "beta").rename(module_file(root, "aaa"))
    sync(client, root)

    assert [module["slug"] for module in modules(client, token)] == ["aaa", "alpha"]


def test_missions_follow_the_module_order(client: TestClient, token: str, root: Path) -> None:
    sync(client, root)
    edit_yaml(
        module_file(root, "alpha"), lambda d: d.update(missions=["sample-answer", "sample-file"])
    )
    sync(client, root)

    (alpha,) = modules(client, token)
    assert [card["slug"] for card in alpha["missions"]] == ["sample-answer", "sample-file"]


def test_unpublished_content_is_hidden(client: TestClient, token: str, root: Path) -> None:
    edit_yaml(mission_file(root, "sample-answer"), lambda d: d.update(status="archived"))
    sync(client, root)
    (alpha,) = modules(client, token)
    assert [card["slug"] for card in alpha["missions"]] == ["sample-file"]
    assert_not_found(client, token, "sample-answer")
    assert_not_found(client, token, "sample-draft")

    # A published mission in a module that is not published is hidden too.
    edit_yaml(module_file(root, "alpha"), lambda d: d.update(status="draft"))
    sync(client, root)
    assert modules(client, token) == []
    assert_not_found(client, token, "sample-file")


def test_removed_mission_is_hidden(client: TestClient, token: str, root: Path) -> None:
    sync(client, root)
    edit_yaml(module_file(root, "alpha"), lambda d: d.update(missions=["sample-file"]))
    shutil.rmtree(root / "missions" / "sample-answer")
    sync(client, root)

    assert_not_found(client, token, "sample-answer")
    (alpha,) = modules(client, token)
    assert [card["slug"] for card in alpha["missions"]] == ["sample-file"]


def test_mission_detail(client: TestClient, token: str, root: Path) -> None:
    sync(client, root)

    body = mission(client, token, "sample-file")

    assert set(body) == DETAIL_FIELDS
    assert body["module"] == {"slug": "alpha", "title": "Módulo de teste A"}
    assert body["briefing"] == (
        "# Arquivo de teste\n\nO arquivo `~/sample.sh` pode ser lido por qualquer usuário.\n"
    )
    assert body["objectives"] == [
        "`~/sample.sh` continua existindo.",
        "Somente o dono pode ler e escrever o arquivo.",
    ]
    assert body["hints"] == ["Veja as permissões atuais com `ls -l`."]
    assert body["version"] == 1
    assert body["requires_answer"] is False
    assert mission(client, token, "sample-answer")["requires_answer"] is True


def test_responses_hold_no_hidden_fields(client: TestClient, token: str, root: Path) -> None:
    sync(client, root)
    responses = [
        request(client, "GET", "/api/modules", token=token).text,
        request(client, "GET", "/api/missions/sample-file", token=token).text,
        request(client, "GET", "/api/missions/sample-answer", token=token).text,
    ]
    # Field names, and text that only appears in setup, conditions, parameters,
    # solutions, counterexamples or the explanation.
    hidden = [f'"{name}"' for name in HIDDEN]
    hidden += ["LAB_PARAM_", "0600", "TOKEN=", "Explicação sintética", "Octal", ".palavra"]
    for text in responses:
        for value in hidden:
            assert value not in text
    for card in modules(client, token)[0]["missions"]:
        assert set(card) == CARD_FIELDS


def test_detail_uses_the_current_version(client: TestClient, token: str, root: Path) -> None:
    sync(client, root)
    (root / "missions" / "sample-file" / "briefing.md").write_text("# Versão 2\n", encoding="utf-8")
    sync(client, root)

    body = mission(client, token, "sample-file")
    assert (body["version"], body["briefing"]) == (2, "# Versão 2\n")
    (alpha,) = modules(client, token)
    assert alpha["missions"][0]["version"] == 2


@pytest.mark.parametrize("slug", ["unknown", "Sample-File", "sample_file", "a" * 65, "%2e%2e"])
def test_unknown_mission(client: TestClient, token: str, root: Path, slug: str) -> None:
    sync(client, root)
    assert_not_found(client, token, slug)
