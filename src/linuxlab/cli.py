"""Command line entry point.

    linuxlab content sync [--content-dir DIR] [--allow-empty] [--source-commit SHA]

Validates content/ and syncs it to the database at DATABASE_URL (see
docs/architecture.md#missions-and-versioning). --source-commit records the commit of
linux-lab-content the directory holds with the sync (content_syncs); the caller vouches
for it. Exit status: 0 when the database matches the content, 1 when the content is
invalid or empty or the sync failed, 2 on a usage error.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from linuxlab.config import get_settings
from linuxlab.content.loader import ContentInvalid, load_content
from linuxlab.content.sync import COMMIT, EmptyContent, SyncReport, sync_content
from linuxlab.db import create_engine, create_sessionmaker


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linuxlab")
    groups = parser.add_subparsers(dest="group", required=True)
    content = groups.add_parser("content", help="mission content")
    commands = content.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="validate content/ and sync it to the database")
    sync.add_argument(
        "--content-dir",
        type=Path,
        help="content directory (default: CONTENT_DIR, or content/ in this repository)",
    )
    sync.add_argument(
        "--allow-empty",
        action="store_true",
        help="when the content directory is empty, archive every module and mission",
    )
    sync.add_argument(
        "--source-commit",
        type=commit,
        help="full commit SHA of linux-lab-content that the content directory holds",
    )
    args = parser.parse_args(argv)
    return content_sync(
        args.content_dir, allow_empty=args.allow_empty, source_commit=args.source_commit
    )


def commit(value: str) -> str:
    if not COMMIT.match(value):
        raise argparse.ArgumentTypeError("must be a full commit SHA (40 lowercase hex digits)")
    return value


def content_sync(
    content_dir: Path | None, *, allow_empty: bool = False, source_commit: str | None = None
) -> int:
    settings = get_settings()
    root = content_dir or settings.content_dir
    try:
        content = load_content(root)
    except ContentInvalid as error:
        for item in error.errors:
            print(f"error: {item}", file=sys.stderr)
        print(f"content is invalid ({len(error.errors)} errors); nothing synced", file=sys.stderr)
        return 1

    async def run() -> SyncReport:
        engine = create_engine(settings.database_url)
        try:
            return await sync_content(
                create_sessionmaker(engine),
                content,
                allow_empty=allow_empty,
                source_commit=source_commit,
            )
        finally:
            await engine.dispose()

    try:
        report = asyncio.run(run())
    except EmptyContent:
        print(
            f"error: {root} has no modules and no missions; nothing synced. "
            "Use --allow-empty to archive every module and mission.",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        # Content errors are caught above; this is the database, and the transaction
        # was rolled back. Only the first line of the cause: SQLAlchemy appends the
        # statement and its parameters, which here include whole missions.
        cause = (str(getattr(error, "orig", None) or error).splitlines() or [""])[0][:200]
        print(
            f"error: sync failed, nothing changed: {type(error).__name__}: {cause}", file=sys.stderr
        )
        return 1

    print(f"content: {len(content.modules)} modules, {len(content.missions)} missions")
    print(
        f"modules: {report.modules_created} created, {report.modules_updated} updated, "
        f"{report.modules_archived} archived"
    )
    print(
        f"missions: {report.missions_created} created, {report.missions_updated} updated, "
        f"{report.missions_archived} archived"
    )
    print(f"versions: {report.versions_created} created")
    print(f"recorded: content commit {source_commit or 'not given'}")
    if not report.changed:
        print("database already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
