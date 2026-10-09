"""Read content/ into validated modules and missions, without touching the database.

    content/
      modules/<slug>.yaml
      missions/<slug>/mission.yaml, and the files it references

Everything is checked before anything is returned: file sizes, UTF-8, YAML (no
duplicate keys, no aliases), the schema, file references (relative, inside the
mission directory, no symlinks) and the links between modules and missions. All
problems are collected and raised together as ContentInvalid. Nothing is executed.
"""

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from linuxlab.content.schema import SLUG, MissionFile, ModuleFile

KIB = 1024
MODULE_FILE_MAX_BYTES = 64 * KIB
MISSION_FILE_MAX_BYTES = 64 * KIB
MARKDOWN_MAX_BYTES = 64 * KIB
SETUP_MAX_BYTES = 32 * KIB
TEST_SCRIPT_MAX_BYTES = 64 * KIB
# mission.yaml and every file it references, each counted once.
MISSION_MAX_BYTES = 256 * KIB

MODULES_DIR = "modules"
MISSIONS_DIR = "missions"
MISSION_FILE = "mission.yaml"
# Placeholder files that keep empty directories in git.
IGNORED_NAMES = frozenset({".gitkeep"})


@dataclass(frozen=True)
class ContentError:
    path: str  # relative to the content directory
    field: str | None
    message: str

    def __str__(self) -> str:
        where = f"{self.path}: {self.field}" if self.field else self.path
        return f"{where}: {self.message}"


class ContentInvalid(Exception):
    def __init__(self, errors: list[ContentError]) -> None:
        super().__init__(f"{len(errors)} content error(s)")
        self.errors = errors


@dataclass(frozen=True)
class Module:
    slug: str
    title: str
    description: str
    status: str
    missions: tuple[str, ...]


@dataclass(frozen=True)
class Mission:
    slug: str
    status: str
    module: str
    position: int  # 1-based, in the module's list
    # The complete specification stored in mission_versions: every field of
    # mission.yaml except status, with referenced files inlined.
    spec: dict[str, Any]
    content_hash: str


@dataclass(frozen=True)
class Content:
    modules: tuple[Module, ...]  # ordered by slug
    missions: tuple[Mission, ...]  # ordered by module, then position

    @property
    def empty(self) -> bool:
        return not self.modules and not self.missions


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(spec: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a mission specification."""
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def load_content(root: Path) -> Content:
    return _Reader(root).read()


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate keys and aliases.

    PyYAML keeps the last of two equal keys silently; aliases let a small file
    expand into a very large document.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                None, None, "aliases are not allowed", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, str | int | bool | float) or key is None:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
        return super().construct_mapping(node, deep)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _clean_loc(loc: tuple[int | str, ...]) -> str | None:
    """Field path of a Pydantic error, without the tags of discriminated unions."""
    parts: list[str] = []
    skip_tag = False
    for item in loc:
        part = str(item)
        if skip_tag:
            skip_tag = False
            continue
        if part == "check":
            skip_tag = True  # followed by the condition type
            continue
        if parts and part == parts[-1] and part in ("all", "any", "not"):
            continue
        parts.append(part)
    return ".".join(parts) or None


class _Reader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: list[ContentError] = []
        self.modules_complete = True

    def error(self, path: Path | str, message: str, field: str | None = None) -> None:
        relative = path if isinstance(path, str) else path.relative_to(self.root).as_posix()
        self.errors.append(ContentError(relative or ".", field, message))

    def read(self) -> Content:
        if self.root.is_symlink() or not self.root.is_dir():
            raise ContentInvalid([ContentError(str(self.root), None, "not a directory")])
        self._check_tree()
        if self.errors:
            raise ContentInvalid(self.errors)

        module_files = self._entries(MODULES_DIR)
        mission_dirs = self._entries(MISSIONS_DIR)
        modules = [m for m in (self._read_module(path) for path in module_files) if m]
        # With a module file unreadable, which missions are listed is unknown.
        self.modules_complete = len(modules) == len(module_files)
        mission_slugs = {path.name for path in mission_dirs if self._is_slug_dir(path)}
        placement = self._place_missions(modules, mission_slugs)
        missions = [
            mission
            for mission in (self._read_mission(path, placement) for path in mission_dirs)
            if mission
        ]
        if self.errors:
            raise ContentInvalid(self.errors)
        missions.sort(key=lambda mission: (mission.module, mission.position))
        return Content(tuple(sorted(modules, key=lambda m: m.slug)), tuple(missions))

    # Layout

    def _check_tree(self) -> None:
        """Only directories and regular files, no symlinks, nothing unexpected on top."""
        for entry in self._list(self.root):
            if entry.name not in (MODULES_DIR, MISSIONS_DIR) and entry.name not in IGNORED_NAMES:
                self.error(entry, "unexpected entry; content/ holds modules/ and missions/")
        stack = [self.root]
        while stack:
            directory = stack.pop()
            for entry in self._list(directory):
                mode = entry.lstat().st_mode
                if stat.S_ISLNK(mode):
                    self.error(entry, "symbolic links are not allowed")
                elif stat.S_ISDIR(mode):
                    stack.append(entry)
                elif not stat.S_ISREG(mode):
                    self.error(entry, "only regular files and directories are allowed")

    def _list(self, directory: Path) -> list[Path]:
        return sorted(directory.iterdir(), key=lambda path: path.name)

    def _entries(self, name: str) -> list[Path]:
        directory = self.root / name
        if not directory.exists():
            return []
        if not directory.is_dir():
            self.error(directory, "must be a directory")
            return []
        return [path for path in self._list(directory) if path.name not in IGNORED_NAMES]

    def _is_slug_dir(self, path: Path) -> bool:
        return path.is_dir() and _valid_slug(path.name)

    # Files

    def _read_text(self, path: Path, limit: int, total: list[int] | None = None) -> str | None:
        size = path.lstat().st_size
        if size > limit:
            self.error(path, f"file is larger than {limit // KIB} KiB")
            return None
        data = path.read_bytes()
        if len(data) > limit:
            self.error(path, f"file is larger than {limit // KIB} KiB")
            return None
        if total is not None:
            total[0] += len(data)
        try:
            return _normalize_newlines(data.decode("utf-8"))
        except UnicodeDecodeError:
            self.error(path, "file is not valid UTF-8")
            return None

    def _read_yaml(self, path: Path, limit: int, total: list[int] | None = None) -> Any:
        text = self._read_text(path, limit, total)
        if text is None:
            return None
        try:
            data = yaml.load(text, Loader=_YamlLoader)
        except yaml.MarkedYAMLError as error:
            mark = error.problem_mark
            where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
            self.error(path, f"invalid YAML: {where}{error.problem}")
            return None
        except yaml.YAMLError:
            self.error(path, "invalid YAML")
            return None
        if not isinstance(data, dict):
            self.error(path, "must be a YAML mapping")
            return None
        return data

    def _validate[M: BaseModel](self, model: type[M], data: Any, path: Path) -> M | None:
        try:
            return model.model_validate(data)
        except ValidationError as error:
            for item in error.errors(include_url=False, include_input=False):
                message = item["msg"].removeprefix("Value error, ")
                self.error(path, message, _clean_loc(item["loc"]))
            return None

    # Modules

    def _read_module(self, path: Path) -> Module | None:
        if path.suffix != ".yaml" or not path.is_file():
            self.error(path, "modules/ holds only <slug>.yaml files")
            return None
        data = self._read_yaml(path, MODULE_FILE_MAX_BYTES)
        if data is None:
            return None
        module = self._validate(ModuleFile, data, path)
        if module is None:
            return None
        if module.slug != path.stem:
            self.error(path, "must match the file name", "slug")
            return None
        return Module(
            module.slug, module.title, module.description, module.status, tuple(module.missions)
        )

    def _place_missions(
        self, modules: list[Module], mission_slugs: set[str]
    ) -> dict[str, tuple[str, int]]:
        """Module and position of each mission. Each mission is in exactly one module."""
        placement: dict[str, tuple[str, int]] = {}
        for module in sorted(modules, key=lambda m: m.slug):
            path = f"{MODULES_DIR}/{module.slug}.yaml"
            for index, slug in enumerate(module.missions):
                field = f"missions.{index}"
                if slug not in mission_slugs:
                    self.error(path, f"no mission directory missions/{slug}", field)
                elif slug in placement:
                    other = placement[slug][0]
                    self.error(path, f"mission {slug} is already in module {other}", field)
                else:
                    placement[slug] = (module.slug, index + 1)
        return placement

    # Missions

    def _read_mission(
        self, directory: Path, placement: dict[str, tuple[str, int]]
    ) -> Mission | None:
        if not directory.is_dir():
            self.error(directory, "missions/ holds only mission directories")
            return None
        if not _valid_slug(directory.name):
            self.error(directory, "directory name must be a slug ([a-z0-9-], up to 64)")
            return None
        path = directory / MISSION_FILE
        if not path.is_file():
            self.error(directory, f"missing {MISSION_FILE}")
            return None
        if directory.name not in placement and self.modules_complete:
            self.error(path, "not listed in any module")
        total = [0]
        data = self._read_yaml(path, MISSION_FILE_MAX_BYTES, total)
        if data is None:
            return None
        mission = self._validate(MissionFile, data, path)
        if mission is None:
            return None
        if mission.slug != directory.name:
            self.error(path, "must match the directory name", "slug")
            return None

        spec = mission.model_dump(mode="json", by_alias=True, exclude_none=True)
        del spec["status"]
        files: dict[str, str] = {}
        ok = True

        def inline(reference: str, field: str, limit: int) -> dict[str, str] | None:
            nonlocal ok
            content = self._read_reference(directory, reference, field, limit, files, total)
            if content is None:
                ok = False
                return None
            return {"path": reference, "content": content}

        spec["briefing"] = inline(mission.briefing, "briefing", MARKDOWN_MAX_BYTES)
        spec["explanation"] = inline(mission.explanation, "explanation", MARKDOWN_MAX_BYTES)
        spec["setup"]["script"] = inline(mission.setup.script, "setup.script", SETUP_MAX_BYTES)
        for key in ("solutions", "counterexamples"):
            for index, script in enumerate(getattr(mission, key)):
                spec[key][index]["script"] = inline(
                    script.script, f"{key}.{index}.script", TEST_SCRIPT_MAX_BYTES
                )
        if not ok:
            return None
        if total[0] > MISSION_MAX_BYTES:
            self.error(
                directory, f"mission files add up to more than {MISSION_MAX_BYTES // KIB} KiB"
            )
            return None
        if directory.name not in placement:
            return None
        module, position = placement[directory.name]
        return Mission(mission.slug, mission.status, module, position, spec, content_hash(spec))

    def _read_reference(
        self,
        directory: Path,
        reference: str,
        field: str,
        limit: int,
        files: dict[str, str],
        total: list[int],
    ) -> str | None:
        """A file named in mission.yaml: a regular file inside the mission directory."""
        mission_file = directory / MISSION_FILE
        if reference in files:
            content = files[reference]
            if len(content.encode("utf-8")) > limit:
                self.error(mission_file, f"file is larger than {limit // KIB} KiB", field)
                return None
            return content
        path = directory
        for part in reference.split("/"):
            path = path / part
            if not path.exists() and not path.is_symlink():
                self.error(mission_file, f"file not found: {reference}", field)
                return None
            if path.is_symlink():
                self.error(mission_file, f"must not go through a symbolic link: {reference}", field)
                return None
        if not path.resolve().is_relative_to(directory.resolve()) or not path.is_file():
            self.error(
                mission_file, f"not a regular file in the mission directory: {reference}", field
            )
            return None
        text = self._read_text(path, limit, total)
        if text is not None:
            files[reference] = text
        return text


def _valid_slug(name: str) -> bool:
    return SLUG.fullmatch(name) is not None
