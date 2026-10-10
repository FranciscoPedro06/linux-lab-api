"""A lab keeps the mission version it was created for, and its owner reads that version.

PostgreSQL with labs on a FakeRuntime.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from tests.auth.support import request, run, sql
from tests.content.support import copy_fixture, edit_yaml, module_file

from .lab_support import MISSION, create_lab, end_lab, get_lab, ready_lab, user

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A copy of the synthetic content that a test can change and sync."""
    return copy_fixture(tmp_path)


def sync(client: TestClient, root: Path) -> None:
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]
    run(client, lambda: sync_content(sessionmaker, load_content(root)))


def pinned(client: TestClient, lab_id: str) -> tuple[str, int, dict[str, str]]:
    rows = sql(
        client,
        "SELECT m.slug, l.mission_version, l.params FROM lab_sessions l"
        " JOIN missions m ON m.id = l.mission_id WHERE l.id = :id",
        id=lab_id,
    )
    assert len(rows) == 1
    slug, version, params = rows[0]
    return slug, version, params


def mission_detail(client: TestClient, token: str, slug: str = MISSION) -> Any:
    return request(client, "GET", f"/api/missions/{slug}", token=token)


def change_briefing(root: Path) -> None:
    (root / "missions" / MISSION / "briefing.md").write_text(
        "# Nova versão\n\nEnunciado alterado.\n", encoding="utf-8"
    )


def test_a_new_version_does_not_change_an_active_lab(client: TestClient, root: Path) -> None:
    sync(client, root)
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab = ready_lab(client, ana)
    params = pinned(client, lab["id"])[2]

    change_briefing(root)
    sync(client, root)

    assert sql(client, "SELECT current_version FROM missions WHERE slug = :s", s=MISSION) == [(2,)]
    assert pinned(client, lab["id"]) == (MISSION, 1, params)
    assert get_lab(client, ana, lab["id"]).json()["mission"]["version"] == 1
    assert create_lab(client, ana).json()["mission"]["version"] == 1
    owner = mission_detail(client, ana).json()
    other = mission_detail(client, bia).json()
    assert (owner["version"], other["version"]) == (1, 2)
    assert "Nova versão" not in owner["briefing"]
    assert "Nova versão" in other["briefing"]

    # Once the lab ends, the owner reads the current version, and a new lab pins it.
    end_lab(client, ana, lab["id"])
    assert mission_detail(client, ana).json()["version"] == 2
    assert ready_lab(client, ana)["mission"]["version"] == 2


def test_an_archived_mission_stays_visible_to_its_active_lab(
    client: TestClient, root: Path
) -> None:
    sync(client, root)
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")
    lab = ready_lab(client, ana)

    edit_yaml(module_file(root, "alpha"), lambda data: data["missions"].remove(MISSION))
    shutil.rmtree(root / "missions" / MISSION)
    sync(client, root)

    assert sql(client, "SELECT status FROM missions WHERE slug = :s", s=MISSION) == [("archived",)]
    owner = mission_detail(client, ana)
    assert owner.status_code == 200
    assert owner.json()["version"] == 1
    assert set(owner.json()) == {
        "slug",
        "title",
        "summary",
        "difficulty",
        "estimated_minutes",
        "tags",
        "version",
        "requires_answer",
        "module",
        "briefing",
        "objectives",
        "hints",
    }
    assert get_lab(client, ana, lab["id"]).json()["mission"] == {
        "slug": MISSION,
        "title": "Arquivo de teste",
        "version": 1,
    }
    assert mission_detail(client, bia).status_code == 404
    assert create_lab(client, bia).json()["error"]["code"] == "mission_not_found"
    # The owner's lab is still returned for the same mission.
    assert create_lab(client, ana).status_code == 200

    end_lab(client, ana, lab["id"])
    assert mission_detail(client, ana).status_code == 404
    assert create_lab(client, ana).status_code == 404


def test_an_ended_lab_does_not_open_an_archived_mission(client: TestClient, root: Path) -> None:
    sync(client, root)
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    end_lab(client, ana, lab["id"])

    edit_yaml(module_file(root, "alpha"), lambda data: data["missions"].remove(MISSION))
    shutil.rmtree(root / "missions" / MISSION)
    sync(client, root)

    assert mission_detail(client, ana).status_code == 404
    assert get_lab(client, ana, lab["id"]).json()["mission"]["slug"] == MISSION
