"""infra/ops/receipts.py: which receipts found off the host describe a confirmed copy.

The script runs on the host, outside the application, so it is loaded from its path.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "ops" / "receipts.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backup_receipts", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receipts = load()

DESTINATION = "backups/host"


def receipt(backup: str, at: str, **changes: Any) -> dict[str, Any]:
    name = f"linuxlab-{backup}.tar.age"
    value = {
        "format_version": 1,
        "backup_id": backup,
        "object": name,
        "size": 1024,
        "sha256": "a" * 64,
        "checksum_object": name + ".sha256",
        "checksum_sha256": "b" * 64,
        "destination": "offsite:" + DESTINATION,
        "rclone_version": "v1.75.2",
        "verified_at": at,
        "status": "verified",
    }
    value.update(changes)
    return value


def put(directory: Path, value: Any, name: str | None = None) -> Path:
    name = name or f"{value['object']}.receipt.json"
    path = directory / name
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
    return path


def select(directory: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str], str]:
    status = receipts.select(directory, DESTINATION)
    out, err = capsys.readouterr()
    return status, out.splitlines(), err


OLD = "20261101T031500Z-0123456789abcdef-scheduled"
NEW = "20261104T031500Z-fedcba9876543210-scheduled"
LATE = "20261102T031500Z-1111111111111111-scheduled"


def test_newest_first_by_verified_at_not_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(tmp_path, receipt(OLD, "2026-11-01T03:15:20Z"))
    put(tmp_path, receipt(NEW, "2026-11-04T03:15:20Z"))
    # Made on the 2nd but confirmed off the host last (sync-backups.sh, later).
    put(tmp_path, receipt(LATE, "2026-11-05T09:15:00Z"))

    status, lines, err = select(tmp_path, capsys)

    assert status == 0 and err == ""
    assert [line.split()[1] for line in lines] == [
        f"linuxlab-{LATE}.tar.age",
        f"linuxlab-{NEW}.tar.age",
        f"linuxlab-{OLD}.tar.age",
    ]
    assert lines[0].split() == [
        "2026-11-05T09:15:00Z",
        f"linuxlab-{LATE}.tar.age",
        "1024",
        "a" * 64,
        "b" * 64,
        LATE,
    ]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": "pending"}, "status is not verified"),
        ({"format_version": 2}, "format_version"),
        ({"format_version": True}, "format_version"),
        ({"object": f"linuxlab-{OLD}.tar.age"}, "another object"),
        ({"backup_id": OLD}, "backup_id"),
        ({"checksum_object": "x.sha256"}, "checksum_object"),
        ({"size": 0}, "size"),
        ({"size": "1024"}, "size"),
        ({"size": 6_000_000_000}, "size"),
        ({"sha256": "A" * 64}, "sha256"),
        ({"checksum_sha256": None}, "checksum_sha256"),
        ({"destination": "offsite:backups/other"}, "another bucket or prefix"),
        ({"destination": "backups/host"}, "destination is malformed"),
        ({"verified_at": "2026-11-04 03:15:20"}, "verified_at"),
        ({"verified_at": None}, "verified_at"),
    ],
)
def test_receipts_that_do_not_describe_a_confirmed_copy_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], changes: dict[str, Any], reason: str
) -> None:
    put(tmp_path, receipt(OLD, "2026-11-01T03:15:20Z"))
    put(
        tmp_path,
        receipt(NEW, "2026-11-04T03:15:20Z", **changes),
        f"linuxlab-{NEW}.tar.age.receipt.json",
    )

    status, lines, err = select(tmp_path, capsys)

    assert status == 0
    assert [line.split()[1] for line in lines] == [f"linuxlab-{OLD}.tar.age"]
    assert f"refused linuxlab-{NEW}.tar.age.receipt.json" in err
    assert reason in err


@pytest.mark.parametrize(
    ("content", "name", "reason"),
    [
        ("not json", None, "not JSON"),
        ("[1, 2]", None, "not a JSON object"),
        ("\xff", None, "not JSON"),
        ("{}", "linuxlab-backup.tar.age.receipt.json", "not named as a receipt"),
        ("{}", "notes.txt", "not named as a receipt"),
        (" " * 70000, None, "larger than"),
    ],
    ids=["text", "array", "latin-1", "bad-name", "other-file", "too-large"],
)
def test_files_that_are_not_receipts_are_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
    name: str | None,
    reason: str,
) -> None:
    put(tmp_path, content, name or f"linuxlab-{NEW}.tar.age.receipt.json")

    status, lines, err = select(tmp_path, capsys)

    assert (status, lines) == (1, [])
    assert reason in err


def test_no_receipt_at_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert select(tmp_path, capsys) == (1, [], "")


def test_a_link_is_not_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(tmp_path, receipt(OLD, "2026-11-01T03:15:20Z"), "real.json")
    (tmp_path / "real.json").rename(tmp_path.parent / f"{tmp_path.name}-real.json")
    try:
        (tmp_path / f"linuxlab-{OLD}.tar.age.receipt.json").symlink_to(
            tmp_path.parent / f"{tmp_path.name}-real.json"
        )
    except OSError:
        pytest.skip("cannot create links here")
    status, lines, err = select(tmp_path, capsys)
    assert (status, lines) == (1, [])
    assert "not a regular file" in err


def checksum_file(tmp_path: Path, line: bytes) -> tuple[Path, Path]:
    name = f"linuxlab-{NEW}.tar.age"
    sums = tmp_path / f"{name}.sha256"
    sums.write_bytes(line)
    value = receipt(NEW, "2026-11-04T03:15:20Z", checksum_sha256=hashlib.sha256(line).hexdigest())
    return sums, put(tmp_path, value)


def test_a_checksum_file_that_matches_its_receipt(tmp_path: Path) -> None:
    sums, path = checksum_file(tmp_path, f"{'a' * 64}  linuxlab-{NEW}.tar.age\n".encode())
    assert receipts.checksum(sums, path) == 0


@pytest.mark.parametrize(
    "line",
    [
        f"{'c' * 64}  linuxlab-{NEW}.tar.age\n",
        f"{'a' * 64}  linuxlab-{OLD}.tar.age\n",
        f"{'a' * 64}  /var/backups/linuxlab/linuxlab-{NEW}.tar.age\n",
        f"{'a' * 64}  linuxlab-{NEW}.tar.age\n{'a' * 64}  other\n",
        f"{'a' * 64}  linuxlab-{NEW}.tar.age",
    ],
)
def test_a_checksum_file_that_does_not_match_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    sums, path = checksum_file(tmp_path, line.encode())
    assert receipts.checksum(sums, path) == 1
    assert "not one line naming" in capsys.readouterr().err


def test_a_checksum_file_changed_after_its_receipt_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sums, path = checksum_file(tmp_path, f"{'a' * 64}  linuxlab-{NEW}.tar.age\n".encode())
    sums.write_bytes(f"{'a' * 64}  linuxlab-{NEW}.tar.age \n".encode())
    assert receipts.checksum(sums, path) == 1
    assert "its SHA-256 is not the receipt's" in capsys.readouterr().err
