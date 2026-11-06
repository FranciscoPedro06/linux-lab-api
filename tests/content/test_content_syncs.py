"""content_syncs: the record of each sync, read by backups (infra/ops/backup.sh)."""

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError

from linuxlab.cli import main
from linuxlab.content.loader import load_content
from linuxlab.content.sync import EmptyContent, sync_content
from tests.auth.support import run, sql

from .support import copy_fixture, edit_yaml, mission_file

pytestmark = pytest.mark.integration

LIB_SH = Path(__file__).resolve().parents[2] / "infra" / "ops" / "lib.sh"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def lib_query(marker: str) -> str:
    """The SQL of infra/ops/lib.sh that follows `-- linuxlab: <marker>`, up to SQL."""
    text = LIB_SH.read_text(encoding="utf-8")
    match = re.search(rf"-- linuxlab: {marker}\n(.*?)\nSQL\n", text, re.DOTALL)
    assert match, marker
    return match.group(1)


def sync(client: TestClient, root: Path, commit: str | None = None) -> None:
    content = load_content(root)
    run(
        client,
        lambda: sync_content(client.app.state.sessionmaker, content, source_commit=commit),  # type: ignore[attr-defined]
    )


def rows(client: TestClient) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in sql(
            client, "SELECT source_commit, missions, fingerprint FROM content_syncs ORDER BY id"
        )
    ]


def backup_reads(client: TestClient) -> tuple[str, str]:
    """What backup.sh compares: the missions now, and the last recorded sync."""
    ((now,),) = sql(client, lib_query("content fingerprint"))
    ((last,),) = sql(client, lib_query("last content sync"))
    return now, last


def test_each_sync_is_recorded_with_its_commit_and_fingerprint(
    client: TestClient, tmp_path: Path
) -> None:
    root = copy_fixture(tmp_path)
    sync(client, root, COMMIT)
    sync(client, root)

    first, second = rows(client)
    assert first[:2] == (COMMIT, 3)
    assert re.fullmatch("[0-9a-f]{64}", first[2])
    # Nothing changed: same fingerprint, and no commit when none was named.
    assert second == (None, 3, first[2])


def test_the_fingerprint_is_lib_sh_s(client: TestClient, tmp_path: Path) -> None:
    root = copy_fixture(tmp_path)
    sync(client, root, COMMIT)

    now, last = backup_reads(client)
    commit, synced_at, missions, fingerprint = last.split(" ")
    assert commit == COMMIT
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", synced_at)
    assert f"{missions} {fingerprint}" == now


def test_a_new_version_changes_the_fingerprint_and_is_recorded(
    client: TestClient, tmp_path: Path
) -> None:
    root = copy_fixture(tmp_path)
    sync(client, root, COMMIT)
    (root / "missions" / "sample-file" / "briefing.md").write_text("# Outro\n", encoding="utf-8")
    sync(client, root, OTHER)

    first, second = rows(client)
    assert second[0] == OTHER
    assert second[2] != first[2]
    now, last = backup_reads(client)
    assert last.startswith(OTHER) and last.endswith(now)


def test_missions_changed_outside_a_sync_no_longer_match(
    client: TestClient, tmp_path: Path
) -> None:
    sync(client, copy_fixture(tmp_path), COMMIT)
    sql(client, "UPDATE missions SET status = 'draft' WHERE slug = 'sample-file'")

    now, last = backup_reads(client)
    assert not last.endswith(now)


def test_a_failed_sync_records_nothing(client: TestClient, tmp_path: Path) -> None:
    with pytest.raises(EmptyContent):
        empty = tmp_path / "empty"
        empty.mkdir()
        sync(client, empty, COMMIT)
    assert rows(client) == []


@pytest.mark.parametrize("commit", ["0123456", COMMIT.upper(), COMMIT + "0", "main"])
def test_only_full_commits(client: TestClient, tmp_path: Path, commit: str) -> None:
    with pytest.raises(ValueError, match="full commit SHA"):
        sync(client, copy_fixture(tmp_path), commit)
    assert rows(client) == []


def test_rows_cannot_be_changed(client: TestClient, tmp_path: Path) -> None:
    sync(client, copy_fixture(tmp_path), COMMIT)
    with pytest.raises(DBAPIError, match="content syncs cannot be changed"):
        sql(client, f"UPDATE content_syncs SET source_commit = '{OTHER}'")
    with pytest.raises(IntegrityError):
        sql(client, "INSERT INTO content_syncs (source_commit, missions, fingerprint)"
            " VALUES ('main', 0, repeat('a', 64))")  # fmt: skip


def test_cli_records_the_source_commit(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = copy_fixture(tmp_path)
    assert main(["content", "sync", "--content-dir", str(root), "--source-commit", COMMIT]) == 0
    assert f"recorded: content commit {COMMIT}" in capsys.readouterr().out
    assert [row[0] for row in rows(client)] == [COMMIT]


def test_cli_refuses_a_partial_commit(
    client: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = copy_fixture(tmp_path)
    edit_yaml(mission_file(root, "sample-file"), lambda d: None)
    with pytest.raises(SystemExit) as exit_info:
        main(["content", "sync", "--content-dir", str(root), "--source-commit", "abc123"])
    assert exit_info.value.code == 2
    assert "full commit SHA" in capsys.readouterr().err
    assert rows(client) == []
