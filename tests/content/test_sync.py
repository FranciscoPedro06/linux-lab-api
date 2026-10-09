"""Sync against PostgreSQL: versions, idempotence, archiving, rollback and concurrency."""

import asyncio
import dataclasses
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, IntegrityError

from linuxlab.content.loader import Content, load_content
from linuxlab.content.sync import EmptyContent, SyncReport, sync_content
from tests.auth.support import run, sql

from .support import copy_fixture, edit_yaml, mission_file, module_file

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


def sync(client: TestClient, content: Content | Path, *, allow_empty: bool = False) -> SyncReport:
    if isinstance(content, Path):
        content = load_content(content)
    loaded = content
    return run(
        client,
        lambda: sync_content(client.app.state.sessionmaker, loaded, allow_empty=allow_empty),  # type: ignore[attr-defined]
    )


def missions(client: TestClient) -> dict[str, tuple[Any, ...]]:
    """slug -> (module slug, position, status, current version)."""
    rows = sql(
        client,
        "SELECT m.slug, mo.slug, m.position, m.status, m.current_version"
        " FROM missions m JOIN modules mo ON mo.id = m.module_id",
    )
    return {row[0]: tuple(row[1:]) for row in rows}


def modules(client: TestClient) -> dict[str, tuple[Any, ...]]:
    rows = sql(client, "SELECT slug, title, description, status FROM modules")
    return {row[0]: tuple(row[1:]) for row in rows}


def versions(client: TestClient, slug: str) -> list[tuple[int, str, dict[str, Any]]]:
    rows = sql(
        client,
        "SELECT v.version, v.content_hash, v.spec FROM mission_versions v"
        " JOIN missions m ON m.id = v.mission_id WHERE m.slug = :slug ORDER BY v.version",
        slug=slug,
    )
    return [tuple(row) for row in rows]


def snapshot(client: TestClient) -> list[Any]:
    return [
        sorted(sql(client, "SELECT * FROM modules")),
        sorted(sql(client, "SELECT * FROM missions")),
        sorted(
            sql(
                client, "SELECT mission_id, version, content_hash, created_at FROM mission_versions"
            )
        ),
    ]


def edit_briefing(root: Path, slug: str, text: str) -> None:
    (root / "missions" / slug / "briefing.md").write_text(text, encoding="utf-8")


# First import and repeated syncs


def test_first_sync_creates_version_1(client: TestClient, root: Path) -> None:
    report = sync(client, root)

    assert report == SyncReport(modules_created=2, missions_created=3, versions_created=3)
    assert modules(client) == {
        "alpha": ("Módulo de teste A", "Conteúdo sintético usado pelos testes.", "published"),
        "beta": ("Módulo de teste B", "Módulo sem missões publicadas.", "published"),
    }
    assert missions(client) == {
        "sample-file": ("alpha", 1, "published", 1),
        "sample-answer": ("alpha", 2, "published", 1),
        "sample-draft": ("beta", 1, "draft", 1),
    }
    content = load_content(root)
    for mission in content.missions:
        ((version, content_hash, spec),) = versions(client, mission.slug)
        assert (version, content_hash, spec) == (1, mission.content_hash, mission.spec)


def test_repeated_sync_changes_nothing(client: TestClient, root: Path) -> None:
    sync(client, root)
    before = snapshot(client)

    report = sync(client, root)

    assert report == SyncReport()
    assert not report.changed
    assert snapshot(client) == before


# Versions


def test_changed_content_creates_a_new_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    ((_, first_hash, first_spec),) = versions(client, "sample-file")

    edit_briefing(root, "sample-file", "# Novo enunciado\n")
    report = sync(client, root)

    assert report == SyncReport(versions_created=1)
    assert missions(client)["sample-file"][3] == 2
    old, new = versions(client, "sample-file")
    assert old == (1, first_hash, first_spec)  # untouched
    assert new[0] == 2
    assert new[1] != first_hash
    assert new[2]["briefing"]["content"] == "# Novo enunciado\n"
    assert len(versions(client, "sample-answer")) == 1


def test_changed_counterexample_creates_a_new_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    (root / "missions" / "sample-file" / "tests" / "counter-empty.sh").write_text(
        "rm -f ~/sample.sh\n"
    )

    assert sync(client, root).versions_created == 1
    assert [v[0] for v in versions(client, "sample-file")] == [1, 2]


def test_returning_to_earlier_content_creates_another_version(
    client: TestClient, root: Path
) -> None:
    sync(client, root)
    original = (root / "missions" / "sample-file" / "briefing.md").read_text(encoding="utf-8")
    edit_briefing(root, "sample-file", "# Outro\n")
    sync(client, root)
    edit_briefing(root, "sample-file", original)

    assert sync(client, root).versions_created == 1

    first, second, third = versions(client, "sample-file")
    assert third[0] == 3
    assert third[1] == first[1] != second[1]
    assert missions(client)["sample-file"][3] == 3


def test_status_change_creates_no_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(mission_file(root, "sample-draft"), lambda d: d.update(status="published"))

    assert sync(client, root) == SyncReport(missions_updated=1)
    assert missions(client)["sample-draft"] == ("beta", 1, "published", 1)


def test_module_changes_create_no_version(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(
        module_file(root, "alpha"),
        lambda d: d.update(
            title="Outro título",
            description="Outra descrição.",
            missions=["sample-answer", "sample-file"],
        ),
    )

    report = sync(client, root)

    assert report == SyncReport(modules_updated=1, missions_updated=2)
    assert modules(client)["alpha"] == ("Outro título", "Outra descrição.", "published")
    found = missions(client)
    assert found["sample-answer"] == ("alpha", 1, "published", 1)
    assert found["sample-file"] == ("alpha", 2, "published", 1)


def test_mission_moved_to_another_module(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(module_file(root, "alpha"), lambda d: d.update(missions=["sample-answer"]))
    edit_yaml(
        module_file(root, "beta"), lambda d: d.update(missions=["sample-file", "sample-draft"])
    )

    assert sync(client, root) == SyncReport(missions_updated=3)
    assert missions(client) == {
        "sample-answer": ("alpha", 1, "published", 1),
        "sample-file": ("beta", 1, "published", 1),
        "sample-draft": ("beta", 2, "draft", 1),
    }


# Removal and reintroduction


def remove_mission(root: Path, slug: str, module: str) -> None:
    edit_yaml(module_file(root, module), lambda d: d["missions"].remove(slug))
    shutil.rmtree(root / "missions" / slug)


def test_removed_mission_is_archived_with_its_versions(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_briefing(root, "sample-answer", "# Segunda versão\n")
    sync(client, root)
    kept = versions(client, "sample-answer")
    remove_mission(root, "sample-answer", "alpha")

    assert sync(client, root) == SyncReport(missions_archived=1)

    assert missions(client)["sample-answer"] == ("alpha", None, "archived", 2)
    assert versions(client, "sample-answer") == kept
    assert sync(client, root) == SyncReport()


def test_removed_module_is_archived(client: TestClient, root: Path) -> None:
    sync(client, root)
    remove_mission(root, "sample-draft", "beta")
    module_file(root, "beta").unlink()

    assert sync(client, root) == SyncReport(modules_archived=1, missions_archived=1)
    assert modules(client)["beta"][2] == "archived"
    assert missions(client)["sample-draft"] == ("beta", None, "archived", 1)
    assert len(versions(client, "sample-draft")) == 1


def test_reintroduced_mission_reuses_its_row(
    client: TestClient, root: Path, tmp_path: Path
) -> None:
    sync(client, root)
    (mission_id,) = sql(client, "SELECT id FROM missions WHERE slug = 'sample-answer'")[0]
    original = copy_fixture(tmp_path / "original")
    remove_mission(root, "sample-answer", "alpha")
    sync(client, root)

    # Same content as before: back to published, no new version.
    assert sync(client, original) == SyncReport(missions_updated=1)
    assert sql(client, "SELECT id FROM missions WHERE slug = 'sample-answer'")[0] == (mission_id,)
    assert missions(client)["sample-answer"] == ("alpha", 2, "published", 1)

    # Different content: a new version on the same row.
    remove_mission(original, "sample-answer", "alpha")
    sync(client, original)
    reintroduced = copy_fixture(tmp_path / "changed")
    edit_briefing(reintroduced, "sample-answer", "# Voltou diferente\n")
    assert sync(client, reintroduced) == SyncReport(missions_updated=1, versions_created=1)
    assert missions(client)["sample-answer"] == ("alpha", 2, "published", 2)


def test_reintroduced_module_reuses_its_row(client: TestClient, root: Path, tmp_path: Path) -> None:
    sync(client, root)
    (module_id,) = sql(client, "SELECT id FROM modules WHERE slug = 'beta'")[0]
    remove_mission(root, "sample-draft", "beta")
    module_file(root, "beta").unlink()
    sync(client, root)

    assert sync(client, copy_fixture(tmp_path / "again")) == SyncReport(
        modules_updated=1, missions_updated=1
    )
    assert sql(client, "SELECT id, status FROM modules WHERE slug = 'beta'") == [
        (module_id, "published")
    ]


# Empty content


def test_empty_content_is_refused(client: TestClient, root: Path, tmp_path: Path) -> None:
    sync(client, root)
    before = snapshot(client)
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(EmptyContent):
        sync(client, empty)
    assert snapshot(client) == before


def test_empty_content_archives_everything_when_allowed(
    client: TestClient, root: Path, tmp_path: Path
) -> None:
    sync(client, root)
    empty = tmp_path / "empty"
    empty.mkdir()

    report = sync(client, empty, allow_empty=True)

    assert report == SyncReport(modules_archived=2, missions_archived=3)
    assert {status for *_, status in modules(client).values()} == {"archived"}
    assert {(position, status) for _, position, status, _ in missions(client).values()} == {
        (None, "archived")
    }
    assert sql(client, "SELECT count(*) FROM mission_versions") == [(3,)]


def test_empty_content_on_an_empty_database(client: TestClient, tmp_path: Path) -> None:
    assert sync(client, tmp_path, allow_empty=True) == SyncReport()


# Atomicity and concurrency


def test_failure_mid_sync_rolls_everything_back(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_yaml(module_file(root, "alpha"), lambda d: d.update(title="Não deve ficar"))
    edit_briefing(root, "sample-file", "# Não deve ficar\n")
    content = load_content(root)
    before = snapshot(client)
    # The last mission breaks a database constraint after the others were written.
    broken = dataclasses.replace(content.missions[-1], content_hash="not-a-hash")
    content = Content(content.modules, (*content.missions[:-1], broken))

    with pytest.raises(IntegrityError):
        sync(client, content)

    assert snapshot(client) == before
    assert modules(client)["alpha"][0] == "Módulo de teste A"


def test_concurrent_syncs_create_each_version_once(client: TestClient, root: Path) -> None:
    content = load_content(root)
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]

    async def both() -> list[SyncReport]:
        return list(await asyncio.gather(*(sync_content(sessionmaker, content) for _ in range(4))))

    reports = run(client, both)

    assert sum(report.versions_created for report in reports) == 3
    assert sum(report.missions_created for report in reports) == 3
    assert sorted(report.changed for report in reports) == [False, False, False, True]
    assert sql(client, "SELECT count(*) FROM mission_versions") == [(3,)]


def test_concurrent_syncs_of_changed_content(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_briefing(root, "sample-file", "# Versão 2\n")
    content = load_content(root)
    sessionmaker = client.app.state.sessionmaker  # type: ignore[attr-defined]

    async def both() -> list[SyncReport]:
        return list(await asyncio.gather(*(sync_content(sessionmaker, content) for _ in range(3))))

    reports = run(client, both)

    assert sorted(report.versions_created for report in reports) == [0, 0, 1]
    assert [v[0] for v in versions(client, "sample-file")] == [1, 2]


# Database rules


def test_versions_cannot_be_updated_or_deleted(client: TestClient, root: Path) -> None:
    sync(client, root)
    for statement in (
        "UPDATE mission_versions SET spec = '{}'",
        "UPDATE mission_versions SET content_hash = repeat('0', 64)",
        "DELETE FROM mission_versions",
    ):
        with pytest.raises(DBAPIError, match="cannot be changed or deleted"):
            sql(client, statement)
    assert sql(client, "SELECT count(*) FROM mission_versions") == [(3,)]


def test_current_version_must_exist(client: TestClient, root: Path) -> None:
    sync(client, root)
    with pytest.raises(IntegrityError, match="fk_missions_current_version_mission_versions"):
        sql(client, "UPDATE missions SET current_version = 5 WHERE slug = 'sample-file'")


def test_current_version_must_belong_to_the_mission(client: TestClient, root: Path) -> None:
    sync(client, root)
    edit_briefing(root, "sample-answer", "# Versão 2\n")
    sync(client, root)
    with pytest.raises(IntegrityError):
        sql(client, "UPDATE missions SET current_version = 2 WHERE slug = 'sample-file'")


def test_positions_are_unique_within_a_module(client: TestClient, root: Path) -> None:
    sync(client, root)
    with pytest.raises(IntegrityError, match="uq_missions_module_id_position"):
        sql(client, "UPDATE missions SET position = 1 WHERE slug = 'sample-answer'")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE missions SET status = 'hidden'",
        "UPDATE missions SET position = NULL WHERE status = 'published'",
        "UPDATE missions SET position = 0",
        "UPDATE modules SET status = 'hidden'",
        "UPDATE modules SET slug = 'Not A Slug' WHERE slug = 'alpha'",
        "UPDATE missions SET slug = 'sample-file' WHERE slug = 'sample-answer'",
    ],
)
def test_catalog_constraints(client: TestClient, root: Path, statement: str) -> None:
    sync(client, root)
    with pytest.raises(IntegrityError):
        sql(client, statement)
