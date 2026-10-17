"""The user's progress in GET /api/modules and GET /api/missions/{slug}.

PostgreSQL, with labs on a FakeRuntime and the probe stand-in.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.auth.support import request
from tests.content.support import copy_fixture
from tests.labs.lab_support import MISSION, end_lab, publish_missions, ready_lab, user
from tests.validation.test_validate_api import validate

from .test_completion_api import NOT_COMPLETED, completions, solve, sync

pytestmark = pytest.mark.integration


def cards(client: TestClient, token: str) -> dict[str, Any]:
    response = request(client, "GET", "/api/modules", token=token)
    assert response.status_code == 200, response.text
    return {card["slug"]: card for module in response.json() for card in module["missions"]}


def detail(client: TestClient, token: str, slug: str = MISSION) -> Any:
    response = request(client, "GET", f"/api/missions/{slug}", token=token)
    assert response.status_code == 200, response.text
    return response.json()


def complete(client: TestClient, token: str) -> dict[str, Any]:
    """Pass a new lab for the mission and end it; return the progress."""
    lab = ready_lab(client, token)
    solve(client, lab)
    body = validate(client, token, lab["id"]).json()
    assert body["newly_completed"] is True
    end_lab(client, token, lab["id"])
    progress: dict[str, Any] = body["progress"]
    return progress


def test_a_user_without_completions_sees_none(client: TestClient) -> None:
    token = user(client, "ana@example.com")
    publish_missions(client)

    assert {slug: card["progress"] for slug, card in cards(client, token).items()} == {
        MISSION: NOT_COMPLETED,
        "sample-answer": NOT_COMPLETED,
    }
    assert detail(client, token)["progress"] == NOT_COMPLETED


def test_a_completion_shows_for_its_user_only(client: TestClient) -> None:
    ana = user(client, "ana@example.com")
    bia = user(client, "bia@example.com")

    progress = complete(client, ana)

    assert progress["status"] == "completed"
    assert progress["completed_version"] == 1
    assert datetime.fromisoformat(progress["completed_at"]) == completions(client)[0][3]
    # Shown after the lab ended, in the list and the detail.
    assert cards(client, ana)[MISSION]["progress"] == progress
    assert cards(client, ana)["sample-answer"]["progress"] == NOT_COMPLETED
    assert detail(client, ana)["progress"] == progress
    # Another user sees their own progress, not Ana's.
    assert cards(client, bia)[MISSION]["progress"] == NOT_COMPLETED
    assert detail(client, bia)["progress"] == NOT_COMPLETED


def test_a_new_version_keeps_the_completion_on_the_earlier_one(
    client: TestClient, tmp_path: Path
) -> None:
    root = copy_fixture(tmp_path)
    sync(client, root)
    ana = user(client, "ana@example.com")
    progress = complete(client, ana)

    (root / "missions" / MISSION / "briefing.md").write_text("# Versão 2\n", encoding="utf-8")
    sync(client, root)

    card = cards(client, ana)[MISSION]
    assert (card["version"], card["progress"]) == (2, progress)
    assert progress["completed_version"] == 1
    body = detail(client, ana)
    assert (body["version"], body["current_version"], body["progress"]) == (2, 2, progress)


def test_the_detail_tells_the_pinned_version_from_the_current_one(
    client: TestClient, tmp_path: Path
) -> None:
    root = copy_fixture(tmp_path)
    sync(client, root)
    ana = user(client, "ana@example.com")
    lab = ready_lab(client, ana)
    (root / "missions" / MISSION / "briefing.md").write_text("# Versão 2\n", encoding="utf-8")
    sync(client, root)

    solve(client, lab)
    progress = validate(client, ana, lab["id"]).json()["progress"]

    body = detail(client, ana)
    # The lab's version 1 is shown; the catalog is at version 2; completed on 1.
    assert (body["version"], body["current_version"]) == (1, 2)
    assert body["progress"] == progress
    assert progress["completed_version"] == 1
    assert cards(client, ana)[MISSION]["version"] == 2
