"""`linuxlab content sync` against PostgreSQL."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from linuxlab.cli import main
from linuxlab.config import DEFAULT_CONTENT_DIR, Settings
from tests.auth.support import sql
from tests.content.support import copy_fixture, edit_yaml, mission_file

pytestmark = pytest.mark.integration

REPOSITORY = Path(__file__).resolve().parents[1]


def sync(*args: str | Path) -> int:
    return main(["content", "sync", *(str(arg) for arg in args)])


def counts(client: TestClient) -> list[tuple[int, int, int]]:
    return sql(
        client,
        "SELECT (SELECT count(*) FROM modules), (SELECT count(*) FROM missions),"
        " (SELECT count(*) FROM mission_versions)",
    )


def test_default_content_dir_is_the_repository_content() -> None:
    assert DEFAULT_CONTENT_DIR == REPOSITORY / "content"
    assert Settings(database_url="postgresql+asyncpg://x").content_dir == DEFAULT_CONTENT_DIR
    settings = Settings(database_url="postgresql+asyncpg://x", content_dir=Path("/srv/content"))
    assert settings.content_dir == Path("/srv/content")


def test_sync_then_sync_again(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = copy_fixture(tmp_path)

    assert sync("--content-dir", root) == 0
    assert capsys.readouterr().out.splitlines() == [
        "content: 2 modules, 3 missions",
        "modules: 2 created, 0 updated, 0 archived",
        "missions: 3 created, 0 updated, 0 archived",
        "versions: 3 created",
    ]

    assert sync("--content-dir", root) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "database already up to date"
    assert counts(client) == [(2, 3, 3)]


def test_invalid_content_changes_nothing(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = copy_fixture(tmp_path)
    edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(difficulty=9))

    assert sync("--content-dir", root) == 1

    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("error: missions/sample-file/mission.yaml: difficulty: ")
    assert err[-1] == "content is invalid (1 errors); nothing synced"
    assert counts(client) == [(0, 0, 0)]


def test_empty_content_needs_allow_empty(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync("--content-dir", copy_fixture(tmp_path)) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    capsys.readouterr()

    assert sync("--content-dir", empty) == 1
    assert "has no modules and no missions; nothing synced" in capsys.readouterr().err
    assert sql(client, "SELECT count(*) FROM missions WHERE status = 'archived'") == [(0,)]

    assert sync("--content-dir", empty, "--allow-empty") == 0
    assert "missions: 0 created, 0 updated, 3 archived" in capsys.readouterr().out
    assert sql(client, "SELECT count(*) FROM missions WHERE status = 'archived'") == [(3,)]
    assert counts(client) == [(2, 3, 3)]


def test_repository_content_is_valid(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """content/ is checked like any other content. It holds no missions yet."""
    status = sync("--content-dir", REPOSITORY / "content")
    err = capsys.readouterr().err
    if status == 1 and "has no modules and no missions" in err:
        return
    assert status == 0, err


def test_missing_content_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync("--content-dir", tmp_path / "absent") == 1
    assert ": not a directory" in capsys.readouterr().err


def test_usage_errors() -> None:
    with pytest.raises(SystemExit) as error:
        main(["content"])
    assert error.value.code == 2
