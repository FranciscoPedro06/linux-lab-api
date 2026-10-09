"""Command line entry point.

    linuxlab content sync [--content-dir DIR] [--allow-empty]

Validates content/ and syncs it to the database at DATABASE_URL (see
docs/architecture.md#missions-and-versioning). Exit status: 0 when the database
matches the content, 1 when the content is invalid or empty or the sync failed,
2 on a usage error.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from linuxlab.config import get_settings
from linuxlab.content.loader import ContentInvalid, load_content
from linuxlab.content.sync import EmptyContent, SyncReport, sync_content
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
    args = parser.parse_args(argv)
    return content_sync(args.content_dir, allow_empty=args.allow_empty)


def content_sync(content_dir: Path | None, *, allow_empty: bool = False) -> int:
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
            return await sync_content(create_sessionmaker(engine), content, allow_empty=allow_empty)
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
    if not report.changed:
        print("database already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
