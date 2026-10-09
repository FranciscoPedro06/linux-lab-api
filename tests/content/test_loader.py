"""Reading content/: layout, files, limits, links between modules and missions, hashing."""

import os
from pathlib import Path

import pytest

from linuxlab.content.loader import (
    MARKDOWN_MAX_BYTES,
    MISSION_FILE_MAX_BYTES,
    MISSION_MAX_BYTES,
    MODULE_FILE_MAX_BYTES,
    SETUP_MAX_BYTES,
    TEST_SCRIPT_MAX_BYTES,
    Content,
    ContentError,
    ContentInvalid,
    canonical_json,
    content_hash,
    load_content,
)

from .support import FIXTURE, copy_fixture, edit_yaml, mission_file, module_file


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


def load_errors(root: Path) -> list[ContentError]:
    with pytest.raises(ContentInvalid) as error:
        load_content(root)
    return error.value.errors


def messages(root: Path) -> list[str]:
    return [str(error) for error in load_errors(root)]


def hashes(content: Content) -> dict[str, str]:
    return {mission.slug: mission.content_hash for mission in content.missions}


def symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("creating symbolic links is not permitted here")


# The fixture


def test_fixture_is_loaded() -> None:
    content = load_content(FIXTURE)

    assert [module.slug for module in content.modules] == ["alpha", "beta"]
    assert content.modules[0].missions == ("sample-file", "sample-answer")
    placed = [(m.module, m.position, m.slug, m.status) for m in content.missions]
    assert placed == [
        ("alpha", 1, "sample-file", "published"),
        ("alpha", 2, "sample-answer", "published"),
        ("beta", 1, "sample-draft", "draft"),
    ]


def test_spec_holds_every_field_and_file_but_not_the_status() -> None:
    mission = load_content(FIXTURE).missions[0]
    spec = mission.spec

    assert "status" not in spec
    assert spec["slug"] == "sample-file"
    assert spec["briefing"]["path"] == "briefing.md"
    assert spec["briefing"]["content"].startswith("# Arquivo de teste\n")
    assert spec["explanation"]["content"] == "Explicação sintética.\n"
    assert "$LAB_PARAM_TOKEN" in spec["setup"]["script"]["content"]
    assert spec["setup"]["user"] == "student"  # default made explicit
    assert spec["params"] == {"token": {"generator": "hex", "length": 8}}
    assert [s["script"]["path"] for s in spec["solutions"]] == [
        "tests/solution-octal.sh",
        "tests/solution-symbolic.sh",
    ]
    assert (
        spec["counterexamples"][0]["script"]["content"]
        == ": > ~/sample.sh\nchmod 600 ~/sample.sh\n"
    )
    assert spec["validation"]["all"][2] == {
        "id": "mode",
        "type": "file_permissions",
        "path": "/home/student/sample.sh",
        "mode": "0600",
    }
    assert mission.content_hash == content_hash(spec)


def test_files_are_never_executed(root: Path) -> None:
    marker = root.parent / "executed"
    (root / "missions" / "sample-file" / "setup.sh").write_text(f"touch {marker}\n")
    load_content(root)
    assert not marker.exists()


# Hashing


def test_hash_is_canonical_json_sha256() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'
    assert content_hash({"b": 1, "a": 2}) == content_hash({"a": 2, "b": 1})
    assert len(content_hash({})) == 64


def test_hash_ignores_key_order_and_line_endings(root: Path) -> None:
    before = hashes(load_content(root))
    path = mission_file(root, "sample-file")
    lines = path.read_text(encoding="utf-8").splitlines()
    # Same document with the top-level keys reordered and CRLF line endings.
    path.write_bytes("\r\n".join(lines[1:] + lines[:1]).encode())
    briefing = root / "missions" / "sample-file" / "briefing.md"
    briefing.write_bytes(briefing.read_bytes().replace(b"\n", b"\r\n"))

    assert hashes(load_content(root)) == before


def test_hash_changes_with_any_versioned_content(root: Path) -> None:
    base = hashes(load_content(root))["sample-file"]
    directory = root / "missions" / "sample-file"

    def changed() -> bool:
        return hashes(load_content(root))["sample-file"] != base

    seen = set()
    for edit in (
        lambda: (directory / "briefing.md").write_text("# Outro\n", encoding="utf-8"),
        lambda: (directory / "explanation.md").write_text("Outra.\n", encoding="utf-8"),
        lambda: (directory / "setup.sh").write_text("true\n", encoding="utf-8"),
        lambda: (directory / "tests" / "counter-empty.sh").write_text("rm ~/sample.sh\n"),
        lambda: (directory / "tests" / "solution-octal.sh").write_text("chmod 0600 ~/sample.sh\n"),
        lambda: edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(hints=["Outra."])),
    ):
        edit()
        assert changed()
        current = hashes(load_content(root))["sample-file"]
        assert current not in seen
        seen.add(current)


def test_hash_ignores_status_module_and_position(root: Path) -> None:
    before = hashes(load_content(root))
    edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(status="draft"))
    edit_yaml(module_file(root, "alpha"), lambda d: d.update(missions=["sample-answer"]))
    edit_yaml(
        module_file(root, "beta"),
        lambda d: d.update(title="Outro título", missions=["sample-file", "sample-draft"]),
    )

    content = load_content(root)

    assert hashes(content) == before
    moved = next(m for m in content.missions if m.slug == "sample-file")
    assert (moved.module, moved.position, moved.status) == ("beta", 1, "draft")


# Layout and links


def test_empty_content(tmp_path: Path) -> None:
    (tmp_path / ".gitkeep").touch()
    assert load_content(tmp_path).empty
    (tmp_path / "modules").mkdir()
    (tmp_path / "missions").mkdir()
    assert load_content(tmp_path).empty


def test_missing_content_directory(tmp_path: Path) -> None:
    assert [e.message for e in load_errors(tmp_path / "absent")] == ["not a directory"]


def test_unexpected_entries(root: Path) -> None:
    (root / "README.md").write_text("x")
    assert messages(root) == ["README.md: unexpected entry; content/ holds modules/ and missions/"]


def test_unexpected_files_in_modules_and_missions(root: Path) -> None:
    (root / "modules" / "notes.txt").write_text("x")
    (root / "missions" / "stray.yaml").write_text("x")
    assert messages(root) == [
        "modules/notes.txt: modules/ holds only <slug>.yaml files",
        "missions/stray.yaml: missions/ holds only mission directories",
    ]


def test_slugs_must_match_file_and_directory_names(root: Path) -> None:
    edit_yaml(module_file(root, "beta"), lambda d: d.update(slug="gamma"))
    edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(slug="sample-other"))

    found = messages(root)
    assert "modules/beta.yaml: slug: must match the file name" in found
    assert "missions/sample-file/mission.yaml: slug: must match the directory name" in found


def test_mission_in_two_modules(root: Path) -> None:
    edit_yaml(module_file(root, "beta"), lambda d: d["missions"].append("sample-file"))
    assert messages(root) == [
        "modules/beta.yaml: missions.1: mission sample-file is already in module alpha"
    ]


def test_module_refers_to_a_missing_mission(root: Path) -> None:
    edit_yaml(module_file(root, "alpha"), lambda d: d["missions"].append("sample-missing"))
    assert messages(root) == [
        "modules/alpha.yaml: missions.2: no mission directory missions/sample-missing"
    ]


def test_mission_not_in_any_module(root: Path) -> None:
    edit_yaml(module_file(root, "beta"), lambda d: d.update(missions=[]))
    assert messages(root) == ["missions/sample-draft/mission.yaml: not listed in any module"]


def test_mission_directory_problems(root: Path) -> None:
    (root / "missions" / "Bad_Name").mkdir()
    (root / "missions" / "no-file").mkdir()
    edit_yaml(module_file(root, "beta"), lambda d: d["missions"].append("no-file"))

    found = messages(root)
    assert "missions/Bad_Name: directory name must be a slug ([a-z0-9-], up to 64)" in found
    assert "missions/no-file: missing mission.yaml" in found


def test_all_errors_are_reported_together(root: Path) -> None:
    edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(difficulty=9))
    edit_yaml(mission_file(root, "sample-answer"), lambda d: d.update(extra=True))
    found = messages(root)
    assert len(found) == 2
    assert found[0].startswith("missions/sample-answer/mission.yaml: extra: ")
    assert found[1].startswith("missions/sample-file/mission.yaml: difficulty: ")


# YAML


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("schema: 1\nschema: 1\n", "duplicate key 'schema'"),
        ("a: &x [1]\nb: *x\n", "aliases are not allowed"),
        ("a: [1\n", "invalid YAML"),
        ("- a\n- b\n", "must be a YAML mapping"),
    ],
)
def test_yaml_problems(root: Path, text: str, expected: str) -> None:
    mission_file(root, "sample-file").write_text(text)
    assert expected in messages(root)[0]


def test_duplicate_keys_in_nested_mappings(root: Path) -> None:
    path = mission_file(root, "sample-file")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("  profile: default\n", "  profile: default\n  profile: default\n")
    )
    assert "duplicate key 'profile'" in messages(root)[0]


def test_files_must_be_utf8(root: Path) -> None:
    (root / "missions" / "sample-file" / "briefing.md").write_bytes(b"\xff\xfe caf\xe9")
    assert messages(root) == ["missions/sample-file/briefing.md: file is not valid UTF-8"]


def test_errors_carry_the_file_and_field(root: Path) -> None:
    edit_yaml(
        mission_file(root, "sample-file"),
        lambda d: d["validation"]["all"][2].update(mode=700),
    )
    (error,) = load_errors(root)
    assert error.path == "missions/sample-file/mission.yaml"
    assert error.field == "validation.all.2.mode"
    assert error.message == "Input should be a valid string"


# File references


def test_referenced_file_must_exist(root: Path) -> None:
    edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(briefing="missing.md"))
    assert messages(root) == [
        "missions/sample-file/mission.yaml: briefing: file not found: missing.md"
    ]


def test_referenced_path_must_be_a_file(root: Path) -> None:
    edit_yaml(mission_file(root, "sample-file"), lambda d: d.update(briefing="tests"))
    assert "not a regular file" in messages(root)[0]


def test_parent_references_are_rejected(root: Path) -> None:
    edit_yaml(
        mission_file(root, "sample-file"),
        lambda d: d.update(briefing="../sample-answer/briefing.md"),
    )
    assert messages(root) == [
        "missions/sample-file/mission.yaml: briefing: "
        "must not contain empty, '.' or '..' components"
    ]


def test_symlinks_are_rejected(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.md"
    outside.write_text("fora do conteúdo")
    briefing = root / "missions" / "sample-file" / "briefing.md"
    briefing.unlink()
    symlink(briefing, outside)
    assert "missions/sample-file/briefing.md: symbolic links are not allowed" in messages(root)


def test_symlinked_directories_are_rejected(root: Path, tmp_path: Path) -> None:
    symlink(root / "missions" / "linked", root / "missions" / "sample-file")
    assert "missions/linked: symbolic links are not allowed" in messages(root)


# Size limits


@pytest.mark.parametrize(
    ("relative", "limit"),
    [
        ("modules/alpha.yaml", MODULE_FILE_MAX_BYTES),
        ("missions/sample-file/mission.yaml", MISSION_FILE_MAX_BYTES),
        ("missions/sample-file/briefing.md", MARKDOWN_MAX_BYTES),
        ("missions/sample-file/explanation.md", MARKDOWN_MAX_BYTES),
        ("missions/sample-file/setup.sh", SETUP_MAX_BYTES),
        ("missions/sample-file/tests/solution-octal.sh", TEST_SCRIPT_MAX_BYTES),
        ("missions/sample-file/tests/counter-empty.sh", TEST_SCRIPT_MAX_BYTES),
    ],
)
def test_file_size_limits(root: Path, relative: str, limit: int) -> None:
    path = root / relative
    data = path.read_bytes()
    # A comment keeps YAML valid. Two-byte characters: bytes are counted, not characters.
    room = limit - len(data) - 1
    path.write_bytes(data + b"#" + ("é" * (room // 2)).encode() + b"x" * (room % 2))
    assert len(path.read_bytes()) == limit
    load_content(root)

    path.write_bytes(path.read_bytes() + b"x")
    assert messages(root) == [f"{relative}: file is larger than {limit // 1024} KiB"]


def test_mission_size_limit(root: Path) -> None:
    directory = root / "missions" / "sample-file"
    names = ("briefing.md", "explanation.md", "setup.sh", "tests/solution-octal.sh")
    for name in (*names, "tests/solution-symbolic.sh", "tests/counter-empty.sh"):
        (directory / name).write_bytes(b"x" * (30 * 1024 if name == "setup.sh" else 60 * 1024))
    assert messages(root) == [
        f"missions/sample-file: mission files add up to more than {MISSION_MAX_BYTES // 1024} KiB"
    ]
