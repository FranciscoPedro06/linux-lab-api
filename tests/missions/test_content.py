"""Static checks of the mission harness, and the content directory under test.

Tests marked `content` read the content directory under test (CONTENT_DIR, which
has no default and must be outside this repository) and fail when it lacks the
initial missions.
The others check the static checks themselves on synthetic content.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from linuxlab.content.loader import load_content
from tests.content.support import FIXTURE, copy_fixture, edit_yaml, mission_file

from .conftest import HARNESS_FIXTURE, REPOSITORY, ContentDirError, content_dir
from .harness import INITIAL_MISSIONS, static_check

PUBLIC_CONTENT = REPOSITORY / "content"


@pytest.mark.content
def test_the_content_directory_holds_the_initial_missions() -> None:
    try:
        root = content_dir()
    except ContentDirError as error:
        pytest.fail(str(error), pytrace=False)
    problems = static_check(root)
    if problems:
        pytest.fail(f"{root}:\n" + "\n".join(problems), pytrace=False)
    slugs = {mission.slug for mission in load_content(root).missions}
    missing = [slug for slug in INITIAL_MISSIONS if slug not in slugs]
    if missing:
        pytest.fail(f"{root} lacks the initial missions {', '.join(missing)}", pytrace=False)


def test_this_repository_publishes_no_missions() -> None:
    # Mission content, solutions included, lives in the private content repository.
    # content/ here only keeps the directory the production image copies.
    files = [path.relative_to(PUBLIC_CONTENT).as_posix() for path in PUBLIC_CONTENT.rglob("*")]
    assert files == [".gitkeep"]


def test_the_harness_reads_only_an_explicit_content_directory_outside_the_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CONTENT_DIR", raising=False)
    with pytest.raises(ContentDirError, match="CONTENT_DIR is not set"):
        content_dir()
    monkeypatch.setenv("CONTENT_DIR", str(PUBLIC_CONTENT))
    with pytest.raises(ContentDirError, match="must be outside this repository"):
        content_dir()
    monkeypatch.setenv("CONTENT_DIR", str(tmp_path))
    assert content_dir() == tmp_path.resolve()


def test_the_image_build_context_leaves_tests_out() -> None:
    ignored = (REPOSITORY / ".dockerignore").read_text(encoding="utf-8").split()
    assert "tests" in ignored


def test_the_synthetic_content_passes_the_static_checks() -> None:
    assert static_check(FIXTURE) == []
    assert static_check(HARNESS_FIXTURE) == []


def broken(tmp_path: Path, change: Callable[[Path], None]) -> list[str]:
    root = copy_fixture(tmp_path)
    change(root)
    return static_check(root)


def edit_mission(change: Callable[[dict[str, Any]], None]) -> Callable[[Path], None]:
    return lambda root: edit_yaml(mission_file(root, "sample-file"), change)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda root: mission_file(root, "sample-file").write_text("schema: [\n"),
            "missions/sample-file/mission.yaml: invalid YAML",
        ),
        (
            edit_mission(lambda data: data.update(briefing="../sample-answer/briefing.md")),
            "missions/sample-file/mission.yaml: briefing:",
        ),
        (
            edit_mission(lambda data: data.update(difficulty=9)),
            "missions/sample-file/mission.yaml: difficulty:",
        ),
        (
            edit_mission(
                lambda data: data["solutions"][1].update(script="tests/solution-octal.sh")
            ),
            "solutions: each solution must use a different script",
        ),
        (
            edit_mission(lambda data: data.update(counterexamples=[])),
            "missions/sample-file/mission.yaml: counterexamples:",
        ),
    ],
)
def test_invalid_content_is_reported_with_its_file_and_field(
    tmp_path: Path, change: Callable[[Path], None], expected: str
) -> None:
    problems = broken(tmp_path, change)

    assert len(problems) == 1, problems
    assert problems[0].startswith("[content] invalid_content: ")
    assert expected in problems[0]


def test_parameters_that_cannot_vary_are_reported(tmp_path: Path) -> None:
    problems = broken(
        tmp_path,
        edit_mission(
            lambda data: data["params"].update(
                color={"generator": "choice", "values": ["red"]},
                count={"generator": "int", "min": 7, "max": 7},
                small={"generator": "int", "min": 0, "max": 1},
                letter={"generator": "hex", "length": 1},
                word={"generator": "word"},
            )
        ),
    )

    assert len(problems) == 1, problems
    assert problems[0].startswith("sample-file ")
    assert problems[0].endswith(
        "[content] params_degenerate: parameters that cannot vary between labs: color, count"
    )
