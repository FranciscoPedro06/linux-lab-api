"""Backup packages of infra/ops/backup.sh: build them, check them, unpack them.

A package is an uncompressed tar archive holding, as regular files and nothing else:

    manifest.json             what was captured, from what, and how to restore it
    database/postgres.dump    pg_dump --format=custom of the deployment's database
    caddy/caddy-data.tar.gz   Caddy's data volume, when the deployment has one

The manifest lists every component with its path, size and SHA-256; a package must
hold exactly manifest.json and those paths, each once. The package's own checksum is
not in the manifest (it would have to hash itself): backup.sh writes it next to the
package, in <package>.sha256.

    package.py build PACKAGE --component NAME=FILE... --fact KEY=VALUE...
    package.py check PACKAGE [--extract DIR]

`check` reads the whole package and fails (status 1, the reason on standard error)
unless it is exactly as described; with --extract it also writes the components under
DIR, which must not hold them yet. On success it prints what restore.sh compares, one
"key value" per line.

Standard library only, Python 3.10 or later: it runs on the host, not in an image.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
CHUNK = 1 << 20

# name: (path in the package, type, required)
COMPONENTS: dict[str, tuple[str, str, bool]] = {
    "database": ("database/postgres.dump", "postgresql-custom-dump", True),
    "caddy-data": ("caddy/caddy-data.tar.gz", "tar-gzip", False),
}

SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]+$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
LABEL = re.compile(r"^[a-z0-9-]+$")
REVISION = re.compile(r"^[0-9a-f]+$")
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
SERVER_VERSION = re.compile(r"^[0-9][0-9A-Za-z .()-]*$")
SERVER_VERSION_NUM = re.compile(r"^[0-9]{5,6}$")
PG_DUMP_VERSION = re.compile(r"^pg_dump \(PostgreSQL\) .+$")

CADDY_CONFIG = (
    "not included: Caddy's /config holds only autosave.json, the Caddyfile adapted to"
    " JSON and rewritten at every start; Caddy starts from the Caddyfile in the"
    " release's edge image, never from autosave.json"
)
GLOBALS = (
    "not included: the only role is linuxlab, created by the postgres image from"
    " POSTGRES_USER when its volume is initialised; the dump is restored with"
    " --no-owner, and there are no grants to other roles"
)
# Why code.content.sha is what it is, by the reason backup.sh gives. Without one (a
# backup.sh before content_syncs existed), CONTENT_SHA_SOURCE.
FINGERPRINT_NOTE = (
    " The fingerprint identifies the missions in this dump (slug, status, current version"
    " and its content_hash, which can be recomputed from a checkout of linux-lab-content)"
)
CONTENT_SHA_SOURCE = (
    "not recorded: content-sync.sh does not store the commit it synced." + FINGERPRINT_NOTE
)
CONTENT_SHA_REASONS = {
    "no-table": "not recorded: the database has no content_syncs table (a schema before"
    " it), so no sync recorded its commit." + FINGERPRINT_NOTE,
    "no-sync": "not recorded: no content sync has run since content_syncs was created."
    + FINGERPRINT_NOTE,
    "no-commit": "not recorded: the last content sync was run without a commit (not by"
    " content-sync.sh)." + FINGERPRINT_NOTE,
    "changed": "not recorded: the missions differ from what the last recorded content"
    " sync left, so its commit would not describe them." + FINGERPRINT_NOTE,
    "recorded": "content_syncs: the commit the last content sync synced (content-sync.sh"
    " checks that the checkout is at it, unchanged), whose missions fingerprint is this one",
}
REQUIRES = [
    "PostgreSQL of the dump's major version or later, and its pg_restore"
    " (the deployment pins postgres:16.15-alpine)",
    "release images built by infra/release/build.sh from code.api.sha and code.web.sha",
    "the role linuxlab, created by the postgres image from POSTGRES_USER",
]
PROVIDED_SEPARATELY = [
    "/etc/linuxlab/production.env: settings and secrets; POSTGRES_PASSWORD may be a new"
    " one, the dump holds no role password",
    "a checkout of linux-lab-content at the commit to sync (code.content)",
    "the release file, or the commits to build it again (code.api.sha, code.web.sha)",
]


class PackageError(Exception):
    """The package is not as described; the message says why."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise PackageError(message)


def copy_and_hash(source: IO[bytes], target: IO[bytes] | None) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK):
        digest.update(chunk)
        size += len(chunk)
        if target is not None:
            target.write(chunk)
    return digest.hexdigest(), size


# Building


def pairs(values: list[str], what: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, sep, rest = value.partition("=")
        expect(bool(sep and key), f"{what} must be KEY=VALUE: {value!r}")
        expect(key not in result, f"{what} {key} given twice")
        result[key] = rest
    return result


class Facts:
    """What backup.sh found, checked one by one as the manifest is written."""

    KNOWN = frozenset(
        {
            "backup_id", "created_at", "label", "release_file",
            "api_sha", "api_image_id", "api_image_revision", "api_running_image_id",
            "web_sha", "edge_image_id", "web_image_revision", "edge_running_image_id",
            "database", "server_version", "server_version_num", "pg_dump_version",
            "alembic_revision", "schema_fingerprint", "content", "roles", "caddy_data",
            "content_sha", "content_synced_at", "content_sha_reason",
        }
    )  # fmt: skip

    def __init__(self, values: dict[str, str]) -> None:
        unknown = sorted(set(values) - self.KNOWN)
        expect(not unknown, f"unknown facts: {', '.join(unknown)}")
        self.values = values

    def get(self, key: str, pattern: re.Pattern[str] | None) -> str:
        expect(key in self.values, f"missing fact {key}")
        value = self.values[key]
        expect(value != "", f"fact {key} is empty")
        expect(pattern is None or bool(pattern.match(value)), f"fact {key} is malformed")
        return value

    def optional(self, key: str, pattern: re.Pattern[str]) -> str | None:
        if self.values.get(key, "") == "":
            return None
        return self.get(key, pattern)

    def roles(self) -> list[dict[str, Any]]:
        roles = []
        for item in self.get("roles", None).split(","):
            name, _, superuser = item.partition(":")
            expect(bool(NAME.match(name)) and superuser in ("true", "false"), "fact roles")
            roles.append({"name": name, "superuser": superuser == "true"})
        return roles

    def content(self) -> tuple[int | None, str | None]:
        value = self.values.get("content", "")
        if value == "":
            return None, None
        count, _, fingerprint = value.partition(" ")
        expect(count.isdigit() and bool(SHA256.match(fingerprint)), "fact content is malformed")
        return int(count), fingerprint

    def content_source(self) -> dict[str, Any]:
        reason = self.values.get("content_sha_reason", "")
        sha = self.optional("content_sha", SHA1)
        synced_at = self.optional("content_synced_at", TIMESTAMP)
        if reason == "":
            expect(sha is None and synced_at is None, "fact content_sha without a reason")
            return {"sha": None, "source": CONTENT_SHA_SOURCE, "synced_at": None}
        expect(reason in CONTENT_SHA_REASONS, "fact content_sha_reason is unknown")
        recorded = reason == "recorded"
        expect(
            (sha is not None) == recorded and (synced_at is not None) == recorded,
            "facts content_sha and content_synced_at go with content_sha_reason=recorded only",
        )
        return {"sha": sha, "source": CONTENT_SHA_REASONS[reason], "synced_at": synced_at}


def build_manifest(facts: Facts, components: list[dict[str, Any]]) -> dict[str, Any]:
    missions, content_fingerprint = facts.content()
    version_num = int(facts.get("server_version_num", SERVER_VERSION_NUM))
    has_caddy = any(component["name"] == "caddy-data" for component in components)
    return {
        "format_version": FORMAT_VERSION,
        "backup": {
            "id": facts.get("backup_id", BACKUP_ID),
            "created_at": facts.get("created_at", TIMESTAMP),
            "label": facts.get("label", LABEL),
        },
        "release": {"file": facts.get("release_file", NAME)},
        "code": {
            "api": {
                "sha": facts.get("api_sha", SHA1),
                "source": "release file, whose image id is checked against the local image",
                "image_id": facts.get("api_image_id", IMAGE_ID),
                "image_revision_label": facts.optional("api_image_revision", SHA1),
                "running_image_id": facts.optional("api_running_image_id", IMAGE_ID),
            },
            "web": {
                "sha": facts.get("web_sha", SHA1),
                "source": "release file, whose edge image id is checked against the local image",
                "image_id": facts.get("edge_image_id", IMAGE_ID),
                "image_revision_label": facts.optional("web_image_revision", SHA1),
                "running_image_id": facts.optional("edge_running_image_id", IMAGE_ID),
            },
            "content": {
                **facts.content_source(),
                "missions": missions,
                "fingerprint": content_fingerprint,
            },
        },
        "postgresql": {
            "database": facts.get("database", NAME),
            "server_version": facts.get("server_version", SERVER_VERSION),
            "server_version_num": version_num,
            "pg_dump_version": facts.get("pg_dump_version", PG_DUMP_VERSION),
            "dump_format": "custom",
            "alembic_revision": facts.optional("alembic_revision", REVISION),
            "schema_fingerprint": facts.get("schema_fingerprint", SHA256),
            "roles": facts.roles(),
            "globals": GLOBALS,
        },
        "caddy": {
            "data": "included" if has_caddy else f"absent: {facts.get('caddy_data', None)}",
            "config": CADDY_CONFIG,
        },
        "components": components,
        "restore": {
            "postgresql_major_at_least": version_num // 10000,
            "requires": REQUIRES,
            "provided_separately": PROVIDED_SEPARATELY,
        },
    }


def member(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def build(package: Path, component_args: list[str], fact_args: list[str]) -> None:
    sources = pairs(component_args, "--component")
    unknown = sorted(set(sources) - set(COMPONENTS))
    expect(not unknown, f"unknown components: {', '.join(unknown)}")
    components: list[dict[str, Any]] = []
    for name, (path, kind, required) in COMPONENTS.items():
        if name not in sources:
            expect(not required, f"component {name} is required")
            continue
        with open(sources[name], "rb") as stream:
            digest, size = copy_and_hash(stream, None)
        components.append(
            {"name": name, "path": path, "type": kind, "size": size, "sha256": digest}
        )
    manifest = build_manifest(Facts(pairs(fact_args, "--fact")), components)
    validate_manifest(manifest)
    body = (json.dumps(manifest, indent=2) + "\n").encode()
    # "x": a package never replaces a file.
    with tarfile.open(package, "x", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(member(MANIFEST, len(body)), io.BytesIO(body))
        for component in components:
            with open(sources[component["name"]], "rb") as stream:
                tar.addfile(member(component["path"], component["size"]), stream)


# Checking


def field(obj: dict[str, Any], key: str, kind: type, where: str) -> Any:
    expect(key in obj, f"manifest: {where}{key} is missing")
    value = obj[key]
    # bool is an int in Python: a size or a version is never true or false.
    right = isinstance(value, kind) and (kind is bool or not isinstance(value, bool))
    expect(right, f"manifest: {where}{key} has the wrong type")
    return value


def matches(value: Any, pattern: re.Pattern[str], where: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    expect(isinstance(value, str) and bool(pattern.match(value)), f"manifest: {where} is malformed")


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    """Checks the manifest's shape and returns its components."""
    expect(isinstance(manifest, dict), "manifest: not a JSON object")
    version = field(manifest, "format_version", int, "")
    expect(version == FORMAT_VERSION, f"manifest: format_version {version} is not supported")
    backup = field(manifest, "backup", dict, "")
    matches(backup.get("id"), BACKUP_ID, "backup.id")
    matches(backup.get("created_at"), TIMESTAMP, "backup.created_at")
    matches(backup.get("label"), LABEL, "backup.label")
    code = field(manifest, "code", dict, "")
    for part in ("api", "web"):
        entry = field(code, part, dict, "code.")
        matches(entry.get("sha"), SHA1, f"code.{part}.sha")
        matches(entry.get("image_id"), IMAGE_ID, f"code.{part}.image_id")
    content = field(code, "content", dict, "code.")
    matches(content.get("sha"), SHA1, "code.content.sha", optional=True)
    matches(content.get("synced_at"), TIMESTAMP, "code.content.synced_at", optional=True)
    matches(content.get("fingerprint"), SHA256, "code.content.fingerprint", optional=True)
    postgresql = field(manifest, "postgresql", dict, "")
    field(postgresql, "database", str, "postgresql.")
    field(postgresql, "server_version_num", int, "postgresql.")
    expect(postgresql.get("dump_format") == "custom", "manifest: postgresql.dump_format")
    revision = postgresql.get("alembic_revision")
    matches(revision, REVISION, "postgresql.alembic_revision", optional=True)
    matches(postgresql.get("schema_fingerprint"), SHA256, "postgresql.schema_fingerprint")
    roles = field(postgresql, "roles", list, "postgresql.")
    expect(
        all(isinstance(role, dict) and isinstance(role.get("name"), str) for role in roles),
        "manifest: postgresql.roles is malformed",
    )
    restore = field(manifest, "restore", dict, "")
    field(restore, "postgresql_major_at_least", int, "restore.")
    components = field(manifest, "components", list, "")
    seen: set[str] = set()
    for index, component in enumerate(components):
        where = f"components[{index}]."
        expect(isinstance(component, dict), f"manifest: {where} is not an object")
        name = field(component, "name", str, where)
        expect(name in COMPONENTS, f"manifest: unknown component {name!r}")
        expect(name not in seen, f"manifest: component {name!r} is listed twice")
        seen.add(name)
        path, kind, _ = COMPONENTS[name]
        expect(component.get("path") == path, f"manifest: component {name!r}: path is not {path}")
        expect(component.get("type") == kind, f"manifest: component {name!r}: type is not {kind}")
        expect(field(component, "size", int, where) >= 0, f"manifest: {where}size is negative")
        matches(component.get("sha256"), SHA256, f"{where}sha256")
    for name, (_, _, required) in COMPONENTS.items():
        expect(not required or name in seen, f"manifest: required component {name!r} is missing")
    return list(components)


def read_manifest(tar: tarfile.TarFile, entry: tarfile.TarInfo) -> Any:
    stream = tar.extractfile(entry)
    expect(stream is not None, "manifest.json cannot be read")
    assert stream is not None
    try:
        return json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError(f"manifest.json is not valid JSON: {error}") from error


def check_entries(entries: list[tarfile.TarInfo]) -> list[str]:
    names = [entry.name for entry in entries]
    for entry in entries:
        expect(not entry.name.startswith("/"), f"entry with an absolute path: {entry.name}")
        expect(".." not in entry.name.split("/"), f"entry with '..' in its path: {entry.name}")
        expect(entry.isreg() and not entry.issparse(), f"entry that is not a file: {entry.name}")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    expect(not duplicates, f"entries that appear more than once: {', '.join(duplicates)}")
    expect(MANIFEST in names, "no manifest.json in the package")
    return names


def check_end(package: Path, offset: int) -> None:
    """A tar archive ends with two blocks of zeros after its last entry (POSIX)."""
    with open(package, "rb") as stream:
        stream.seek(offset)
        rest = stream.read()
    expect(
        len(rest) >= 2 * tarfile.BLOCKSIZE and not rest.strip(b"\0"),
        "the archive does not end as a tar archive ends: it was cut or extended",
    )


def check(package: Path, extract: Path | None, out: Callable[[str], None]) -> None:
    try:
        with tarfile.open(package, "r:") as tar:
            entries = tar.getmembers()
            check_end(package, tar.offset)
            names = check_entries(entries)
            manifest = read_manifest(tar, entries[names.index(MANIFEST)])
            components = validate_manifest(manifest)
            expected = {MANIFEST} | {component["path"] for component in components}
            unexpected = sorted(set(names) - expected)
            missing = sorted(expected - set(names))
            expect(not unexpected, f"entries the manifest does not list: {', '.join(unexpected)}")
            expect(not missing, f"components missing from the package: {', '.join(missing)}")
            for component in components:
                check_component(tar, entries[names.index(component["path"])], component, extract)
    except (tarfile.TarError, EOFError) as error:
        raise PackageError(f"the archive cannot be read to its end: {error}") from error

    postgresql = manifest["postgresql"]
    content = manifest["code"]["content"]
    out(f"backup_id {manifest['backup']['id']}")
    out(f"database_path {COMPONENTS['database'][0]}")
    out(f"alembic_revision {postgresql.get('alembic_revision') or '-'}")
    out(f"schema_fingerprint {postgresql['schema_fingerprint']}")
    if content.get("fingerprint"):
        out(f"content {content.get('missions')} {content['fingerprint']}")
    else:
        out("content -")
    out(f"content_sha {content.get('sha') or '-'}")
    out(f"postgresql_major_at_least {manifest['restore']['postgresql_major_at_least']}")


def check_component(
    tar: tarfile.TarFile, entry: tarfile.TarInfo, component: dict[str, Any], extract: Path | None
) -> None:
    path = component["path"]
    expect(entry.size == component["size"], f"{path}: {entry.size} bytes, manifest says otherwise")
    source = tar.extractfile(entry)
    expect(source is not None, f"{path} cannot be read")
    assert source is not None
    with contextlib.ExitStack() as stack:
        target = None
        if extract is not None:
            (extract / path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = stack.enter_context(open(extract / path, "xb"))
        digest, size = copy_and_hash(source, target)
    expect(size == component["size"], f"{path}: truncated")
    expect(digest == component["sha256"], f"{path} does not match its SHA-256 in the manifest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backup packages of infra/ops/backup.sh.")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="write a new package")
    build_parser.add_argument("package", type=Path)
    build_parser.add_argument("--component", action="append", default=[])
    build_parser.add_argument("--fact", action="append", default=[])
    check_parser = commands.add_parser("check", help="check a package, optionally unpack it")
    check_parser.add_argument("package", type=Path)
    check_parser.add_argument("--extract", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build(args.package, args.component, args.fact)
        else:
            check(args.package, args.extract, print)
    except PackageError as error:
        print(f"error: {args.package}: {error}", file=sys.stderr)
        return 1
    except FileExistsError as error:
        print(f"error: {error.filename} already exists; nothing replaced", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"error: {args.package}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
