"""Synthetic content for tests: tests/fixtures/content, copied so a test can change it."""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "content"


def copy_fixture(destination: Path) -> Path:
    root = destination / "content"
    shutil.copytree(FIXTURE, root)
    return root


def read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def edit_yaml(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = read_yaml(path)
    change(data)
    write_yaml(path, data)


def mission_file(root: Path, slug: str) -> Path:
    return root / "missions" / slug / "mission.yaml"


def module_file(root: Path, slug: str) -> Path:
    return root / "modules" / f"{slug}.yaml"


def mission_data(slug: str = "sample-file") -> dict[str, Any]:
    """A valid mission.yaml from the fixture, as parsed YAML."""
    return read_yaml(mission_file(FIXTURE, slug))
