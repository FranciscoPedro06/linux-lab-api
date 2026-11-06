"""Receipts of the copies off the host (infra/ops/remote.sh), read back on a new host.

infra/ops/restore-offsite.sh downloads the receipts it finds under the destination's
prefix into a directory, each under its object's name, and asks this script which of
them describe a confirmed copy, newest first:

    receipts.py select DIR --destination BUCKET/PREFIX
    receipts.py checksum FILE --receipt RECEIPT

`select` prints one line per valid receipt, newest first (verified_at, then name):

    <verified_at> <object> <size> <sha256> <checksum_sha256> <backup_id>

and one line per refused receipt on standard error, with the reason. A receipt is only
a claim: the caller still reads the objects it names back in full and compares them.
Exit status 0 when at least one receipt is valid, 1 otherwise.

`checksum` checks a downloaded checksum file against a receipt: the file's SHA-256 is
the receipt's checksum_sha256, and its one line names the receipt's object with the
receipt's sha256. Exit status 0 when it does, 1 otherwise (the reason on standard
error).

Standard library only, Python 3.10 or later: it runs on the host, not in an image.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
# Largest receipt or checksum file read: both are a few hundred bytes.
MAX_BYTES = 65536
# remote.sh never uploads more than this in one object (REMOTE_MAX_BYTES).
MAX_OBJECT_BYTES = 5_000_000_000
RECEIPT = re.compile(
    r"^(linuxlab-([0-9]{8}T[0-9]{6}Z-[0-9a-f]+-[a-z0-9-]+)\.tar\.age)\.receipt\.json$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
DESTINATION = re.compile(r"^[A-Za-z0-9_-]+:([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)$")


class Refused(Exception):
    """The receipt does not describe a confirmed copy; the message says why."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Refused(message)


def read_small(path: Path) -> bytes:
    with open(path, "rb") as source:
        data = source.read(MAX_BYTES + 1)
    expect(len(data) <= MAX_BYTES, f"larger than {MAX_BYTES} bytes")
    return data


def integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse(path: Path, destination: str) -> dict[str, Any]:
    """The receipt at PATH, checked; raises Refused."""
    match = RECEIPT.match(path.name)
    expect(match is not None, "not named as a receipt of a backup")
    assert match is not None
    obj, backup_id = match.group(1), match.group(2)
    try:
        receipt = json.loads(read_small(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refused("not JSON") from None
    expect(isinstance(receipt, dict), "not a JSON object")
    version = receipt.get("format_version")
    expect(version == FORMAT_VERSION and integer(version), "format_version is not 1")
    expect(receipt.get("status") == "verified", "status is not verified")
    expect(receipt.get("object") == obj, "names another object than its own name says")
    expect(receipt.get("backup_id") == backup_id, "backup_id does not match the object")
    expect(
        receipt.get("checksum_object") == obj + ".sha256", "checksum_object is not <object>.sha256"
    )
    size = receipt.get("size")
    expect(integer(size) and 0 < size <= MAX_OBJECT_BYTES, "size is not a byte count")
    for key in ("sha256", "checksum_sha256"):
        value = receipt.get(key)
        expect(isinstance(value, str) and bool(SHA256.match(value)), f"{key} is not a SHA-256")
    where = receipt.get("destination")
    found = DESTINATION.match(where) if isinstance(where, str) else None
    expect(found is not None, "destination is malformed")
    assert found is not None
    expect(found.group(1) == destination, f"made for another bucket or prefix ({found.group(1)})")
    stamp = receipt.get("verified_at")
    try:
        expect(isinstance(stamp, str), "verified_at is not a UTC time")
        datetime.datetime.strptime(stamp, TIMESTAMP)
    except ValueError:
        raise Refused("verified_at is not a UTC time") from None
    return dict(receipt)


def select(directory: Path, destination: str) -> int:
    valid: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        try:
            expect(path.is_file() and not path.is_symlink(), "not a regular file")
            valid.append(parse(path, destination))
        except (Refused, OSError) as error:
            print(f"refused {path.name}: {error}", file=sys.stderr)
    # The time is to the second; the name (the backup's time and id) breaks ties.
    valid.sort(key=lambda r: (r["verified_at"], r["object"]), reverse=True)
    for r in valid:
        keys = ("verified_at", "object", "size", "sha256", "checksum_sha256", "backup_id")
        print(" ".join(str(r[key]) for key in keys))
    return 0 if valid else 1


def checksum(path: Path, receipt_path: Path) -> int:
    try:
        receipt = json.loads(read_small(receipt_path).decode("utf-8"))
        data = read_small(path)
        digest = hashlib.sha256(data).hexdigest()
        expect(digest == receipt["checksum_sha256"], "its SHA-256 is not the receipt's")
        line = data.decode("ascii")
        expected = f"{receipt['sha256']}  {receipt['object']}\n"
        expect(line == expected, "it is not one line naming the receipt's object and SHA-256")
    except (Refused, OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
        print(f"error: {path.name}: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Receipts of the copies off the host.")
    commands = parser.add_subparsers(dest="command", required=True)
    select_parser = commands.add_parser("select", help="valid receipts, newest first")
    select_parser.add_argument("directory", type=Path)
    select_parser.add_argument("--destination", required=True, help="BUCKET/PREFIX")
    checksum_parser = commands.add_parser("checksum", help="check a checksum file")
    checksum_parser.add_argument("file", type=Path)
    checksum_parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "select":
        return select(args.directory, args.destination)
    return checksum(args.file, args.receipt)


if __name__ == "__main__":
    sys.exit(main())
