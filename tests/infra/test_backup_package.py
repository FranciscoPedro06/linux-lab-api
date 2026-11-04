"""infra/ops/package.py: what a backup package must be, checked with real tar archives.

The script runs on the host, outside the application, so it is loaded from its path.
"""

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "ops" / "package.py"
DUMP = b"PGDMP custom-format dump " * 100
CADDY = b"\x1f\x8b caddy data " * 10


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backup_package", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package = load()

FACTS = {
    "backup_id": "20261104T031500Z-0123456789abcdef",
    "created_at": "2026-11-04T03:15:00Z",
    "label": "scheduled",
    "release_file": "release-0123456789ab-fedcba987654.env",
    "api_sha": "0123456789abcdef0123456789abcdef01234567",
    "api_image_id": "sha256:" + "a" * 64,
    "api_image_revision": "0123456789abcdef0123456789abcdef01234567",
    "api_running_image_id": "",
    "web_sha": "fedcba9876543210fedcba9876543210fedcba98",
    "edge_image_id": "sha256:" + "b" * 64,
    "web_image_revision": "",
    "edge_running_image_id": "sha256:" + "b" * 64,
    "database": "linuxlab",
    "server_version": "16.15",
    "server_version_num": "160015",
    "pg_dump_version": "pg_dump (PostgreSQL) 16.15",
    "alembic_revision": "e6b40c7d9a18",
    "schema_fingerprint": "c" * 64,
    "content": "5 " + "d" * 64,
    "roles": "linuxlab:true",
    "caddy_data": "",
}


def build(tmp_path: Path, *, caddy: bool = True, facts: dict[str, str] | None = None) -> Path:
    (tmp_path / "postgres.dump").write_bytes(DUMP)
    (tmp_path / "caddy-data.tar.gz").write_bytes(CADDY)
    components = [f"database={tmp_path / 'postgres.dump'}"]
    if caddy:
        components.append(f"caddy-data={tmp_path / 'caddy-data.tar.gz'}")
    target = tmp_path / "linuxlab.tar"
    package.build(target, components, [f"{k}={v}" for k, v in {**FACTS, **(facts or {})}.items()])
    return target


def entries(path: Path) -> list[tuple[str, bytes]]:
    with tarfile.open(path) as tar:
        result = []
        for member in tar.getmembers():
            stream = tar.extractfile(member)
            assert stream is not None
            result.append((member.name, stream.read()))
        return result


def manifest_of(path: Path) -> dict[str, Any]:
    return dict(json.loads(dict(entries(path))["manifest.json"]))


def write(
    path: Path, members: list[tuple[str, bytes]], extra: list[tarfile.TarInfo] | None = None
) -> None:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)


def rewrite(path: Path, change: Any) -> None:
    """Writes the package again with its manifest changed by change(manifest)."""
    members = entries(path)
    manifest = json.loads(dict(members)["manifest.json"])
    change(manifest)
    body = json.dumps(manifest).encode()
    write(path, [(n, body if n == "manifest.json" else d) for n, d in members])


def check(path: Path, extract: Path | None = None) -> list[str]:
    lines: list[str] = []
    package.check(path, extract, lines.append)
    return lines


def refused(path: Path, reason: str) -> None:
    with pytest.raises(package.PackageError, match=reason):
        check(path)


def test_a_built_package_checks_and_says_what_restore_compares(tmp_path: Path) -> None:
    lines = check(build(tmp_path))

    assert lines == [
        "backup_id 20261104T031500Z-0123456789abcdef",
        "database_path database/postgres.dump",
        "alembic_revision e6b40c7d9a18",
        f"schema_fingerprint {'c' * 64}",
        f"content 5 {'d' * 64}",
        "postgresql_major_at_least 16",
    ]


def test_the_package_holds_the_manifest_and_the_components_as_regular_files(
    tmp_path: Path,
) -> None:
    path = build(tmp_path)

    with tarfile.open(path) as tar:
        members = tar.getmembers()
    assert [(m.name, m.isreg(), m.mode) for m in members] == [
        ("manifest.json", True, 0o600),
        ("database/postgres.dump", True, 0o600),
        ("caddy/caddy-data.tar.gz", True, 0o600),
    ]


def test_the_manifest_records_the_components_versions_and_requirements(tmp_path: Path) -> None:
    manifest = manifest_of(build(tmp_path))

    assert manifest["format_version"] == 1
    assert manifest["components"] == [
        {
            "name": "database",
            "path": "database/postgres.dump",
            "type": "postgresql-custom-dump",
            "size": len(DUMP),
            "sha256": hashlib.sha256(DUMP).hexdigest(),
        },
        {
            "name": "caddy-data",
            "path": "caddy/caddy-data.tar.gz",
            "type": "tar-gzip",
            "size": len(CADDY),
            "sha256": hashlib.sha256(CADDY).hexdigest(),
        },
    ]
    assert manifest["code"]["api"]["sha"] == FACTS["api_sha"]
    assert manifest["code"]["api"]["running_image_id"] is None
    assert manifest["code"]["web"]["image_revision_label"] is None
    assert manifest["code"]["content"]["sha"] is None
    assert "not recorded" in manifest["code"]["content"]["source"]
    assert manifest["postgresql"]["roles"] == [{"name": "linuxlab", "superuser": True}]
    assert manifest["postgresql"]["server_version_num"] == 160015
    assert manifest["restore"]["postgresql_major_at_least"] == 16
    assert any("production.env" in item for item in manifest["restore"]["provided_separately"])
    assert manifest["caddy"]["data"] == "included"


def test_without_caddy_data_the_manifest_says_why(tmp_path: Path) -> None:
    manifest = manifest_of(
        build(tmp_path, caddy=False, facts={"caddy_data": "no volume x_caddy-data"})
    )

    assert [c["name"] for c in manifest["components"]] == ["database"]
    assert manifest["caddy"]["data"] == "absent: no volume x_caddy-data"


@pytest.mark.parametrize(
    ("fact", "value"),
    [
        ("api_sha", "0123"),
        ("api_image_id", "sha256:stub"),
        ("alembic_revision", "head; DROP"),
        ("schema_fingerprint", ""),
        ("server_version_num", "16"),
        ("roles", "linuxlab:yes"),
    ],
)
def test_a_malformed_fact_writes_no_package(tmp_path: Path, fact: str, value: str) -> None:
    with pytest.raises(package.PackageError, match=f"fact {fact}"):
        build(tmp_path, facts={fact: value})

    assert not (tmp_path / "linuxlab.tar").exists()


def test_a_package_never_replaces_a_file(tmp_path: Path) -> None:
    (tmp_path / "linuxlab.tar").write_bytes(b"earlier")

    with pytest.raises(FileExistsError):
        build(tmp_path)
    assert (tmp_path / "linuxlab.tar").read_bytes() == b"earlier"


def test_extract_writes_exactly_the_components(tmp_path: Path) -> None:
    path = build(tmp_path)
    target = tmp_path / "out"
    target.mkdir()

    check(path, target)

    assert sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()) == [
        "caddy/caddy-data.tar.gz",
        "database/postgres.dump",
    ]
    assert (target / "database/postgres.dump").read_bytes() == DUMP


def test_no_manifest(tmp_path: Path) -> None:
    path = build(tmp_path)
    write(path, [(n, d) for n, d in entries(path) if n != "manifest.json"])
    refused(path, "no manifest.json")


def test_a_manifest_that_is_not_json(tmp_path: Path) -> None:
    path = build(tmp_path)
    write(path, [(n, b"{not json" if n == "manifest.json" else d) for n, d in entries(path)])
    refused(path, "not valid JSON")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda m: m.update(format_version=2), "format_version 2 is not supported"),
        (lambda m: m.update(format_version=True), "format_version has the wrong type"),
        (lambda m: m.pop("postgresql"), "postgresql is missing"),
        (lambda m: m["code"]["api"].update(sha="main"), "code.api.sha is malformed"),
        (lambda m: m["components"].pop(0), "required component 'database' is missing"),
        (lambda m: m["components"].append(dict(m["components"][0])), "listed twice"),
        (lambda m: m["components"][0].update(path="/etc/passwd"), "path is not"),
        (lambda m: m["components"][0].update(name="secrets"), "unknown component"),
        (lambda m: m["components"][0].update(size=True), "size has the wrong type"),
    ],
)
def test_a_malformed_manifest(tmp_path: Path, change: Any, reason: str) -> None:
    path = build(tmp_path)
    rewrite(path, change)
    refused(path, reason)


def test_a_component_missing_from_the_package(tmp_path: Path) -> None:
    path = build(tmp_path)
    write(path, [(n, d) for n, d in entries(path) if n != "caddy/caddy-data.tar.gz"])
    refused(path, "components missing from the package: caddy/caddy-data.tar.gz")


def test_a_component_that_appears_twice(tmp_path: Path) -> None:
    path = build(tmp_path)
    members = entries(path)
    write(path, [*members, ("database/postgres.dump", DUMP)])
    refused(path, "more than once: database/postgres.dump")


def test_a_component_whose_content_changed(tmp_path: Path) -> None:
    path = build(tmp_path)
    changed = DUMP.replace(b"dump", b"dumq", 1)
    write(path, [(n, changed if n == "database/postgres.dump" else d) for n, d in entries(path)])
    refused(path, "does not match its SHA-256")


def test_a_component_of_another_size(tmp_path: Path) -> None:
    path = build(tmp_path)
    write(path, [(n, d + b"x" if n == "database/postgres.dump" else d) for n, d in entries(path)])
    refused(path, "database/postgres.dump: .* bytes")


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("/etc/cron.d/backdoor", "absolute path"),
        ("../outside", "'..' in its path"),
        ("database/../../outside", "'..' in its path"),
        ("notes.txt", "does not list: notes.txt"),
    ],
)
def test_an_entry_outside_the_layout(tmp_path: Path, name: str, reason: str) -> None:
    path = build(tmp_path)
    write(path, [*entries(path), (name, b"x")])
    refused(path, reason)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE])
def test_an_entry_that_is_not_a_regular_file(tmp_path: Path, kind: bytes) -> None:
    path = build(tmp_path)
    link = tarfile.TarInfo("database")
    link.type = kind
    link.linkname = "/etc"
    write(path, entries(path), [link])
    refused(path, "not a file: database")


def test_nothing_is_extracted_from_a_package_that_does_not_check(tmp_path: Path) -> None:
    path = build(tmp_path)
    write(path, [*entries(path), ("../outside", b"x")])
    target = tmp_path / "out"
    target.mkdir()

    with pytest.raises(package.PackageError):
        check(path, target)
    assert list(target.iterdir()) == []
    assert not (tmp_path / "outside").exists()


def data_end(path: Path) -> int:
    with tarfile.open(path) as tar:
        tar.getmembers()
        return int(tar.offset)


@pytest.mark.parametrize("where", ["inside an entry", "at the end-of-archive blocks"])
def test_a_truncated_package(tmp_path: Path, where: str) -> None:
    path = build(tmp_path)
    end = data_end(path)
    cut = end - 700 if where == "inside an entry" else end
    path.write_bytes(path.read_bytes()[:cut])

    with pytest.raises(package.PackageError):
        check(path)


def test_a_package_with_data_after_its_end(tmp_path: Path) -> None:
    path = build(tmp_path)
    path.write_bytes(path.read_bytes() + b"appended")
    refused(path, "does not end as a tar archive ends")


def test_not_a_tar_archive(tmp_path: Path) -> None:
    path = tmp_path / "linuxlab.tar"
    path.write_bytes(DUMP)
    with pytest.raises(package.PackageError):
        check(path)


def test_the_command_line_reports_a_refusal_with_status_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = build(tmp_path)
    write(path, [(n, d) for n, d in entries(path) if n != "manifest.json"])

    assert package.main(["check", str(path)]) == 1
    assert "no manifest.json" in capsys.readouterr().err
