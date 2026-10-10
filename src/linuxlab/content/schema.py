"""Schema of module and mission files. See docs/missions.md.

The models check structure only. Nothing in a mission is executed or compiled here:
setup scripts, conditions and parameters are validated as data and stored for the
increments that use them. Models are strict: values are never coerced (an unquoted
`mode: 700` is an integer and is refused), and unknown fields are rejected.
"""

import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    model_validator,
)

SCHEMA_VERSION = 1

MAX_CONDITIONS = 50
MAX_CONDITION_DEPTH = 5

Status = Literal["draft", "published", "archived"]

# Modules, missions, condition ids and tags.
SLUG = re.compile(r"[a-z0-9-]{1,64}")
Slug = Annotated[str, Field(pattern=rf"^{SLUG.pattern}$")]
ParamName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
# Every generated parameter value, and every value a `choice` can produce.
PARAM_VALUE = re.compile(r"[a-z0-9_-]{1,64}")
ParamValue = Annotated[str, Field(pattern=rf"^{PARAM_VALUE.pattern}$")]
# Lab images a mission can ask for. Each alias is resolved by platform configuration:
# `base` is LAB_IMAGE. Unknown aliases are refused here, so a mission that names one
# can never be synced.
ImageAlias = Literal["base"]
AccountName = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")]
Mode = Annotated[str, Field(pattern=r"^[0-7]{4}$")]

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _relative_path(value: str) -> str:
    """A file inside the mission directory, written as a relative POSIX path."""
    if "\\" in value or "\x00" in value:
        raise ValueError("must be a relative path with forward slashes")
    if value.startswith("/"):
        raise ValueError("must be relative to the mission directory")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("must not contain empty, '.' or '..' components")
    return value


def _schema_version(value: Any) -> Any:
    """Literal[1] compares by equality, so True and 1.0 would pass as 1."""
    if type(value) is not int:
        raise ValueError(f"must be the integer {SCHEMA_VERSION}")
    return value


def _container_path(value: str) -> str:
    if not value.startswith("/") or "\x00" in value:
        raise ValueError("must be an absolute path inside the lab")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_schema_version)]
Text = Annotated[str, AfterValidator(_not_blank)]
FilePath = Annotated[str, Field(max_length=255), AfterValidator(_relative_path)]
ContainerPath = Annotated[str, Field(max_length=4096), AfterValidator(_container_path)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


def _exactly_one(model: BaseModel, fields: tuple[str, ...]) -> None:
    given = [name for name in fields if getattr(model, name) is not None]
    if len(given) != 1:
        raise ValueError(f"exactly one of {', '.join(fields)} is required")


# Module file


class ModuleFile(_Strict):
    schema_: SchemaVersion = Field(alias="schema")
    slug: Slug
    title: Text
    description: Text
    status: Status
    # The missions of this module, in order. The only place where either is defined.
    missions: list[Slug]

    @model_validator(mode="after")
    def _unique_missions(self) -> Self:
        if len(set(self.missions)) != len(self.missions):
            raise ValueError("missions: a mission is listed more than once")
        return self


# Conditions


class _Condition(_Strict):
    id: Slug
    fail_message: Text | None = None
    pass_message: Text | None = None


class FileExists(_Condition):
    type: Literal["file_exists"]
    path: ContainerPath
    follow_symlinks: bool = False


class DirectoryExists(_Condition):
    type: Literal["directory_exists"]
    path: ContainerPath
    follow_symlinks: bool = False


class PathAbsent(_Condition):
    type: Literal["path_absent"]
    path: ContainerPath


class FileContent(_Condition):
    type: Literal["file_content"]
    path: ContainerPath
    equals: str | None = None
    contains: Text | None = None
    not_contains: Text | None = None
    # RE2 syntax. Compiled by the validation engine, not here.
    regex: Text | None = None
    trim: bool = True

    @model_validator(mode="after")
    def _one_comparison(self) -> Self:
        _exactly_one(self, ("equals", "contains", "not_contains", "regex"))
        return self


class FilePermissions(_Condition):
    type: Literal["file_permissions"]
    path: ContainerPath
    mode: Mode


class FileOwner(_Condition):
    type: Literal["file_owner"]
    path: ContainerPath
    user: AccountName


class FileGroup(_Condition):
    type: Literal["file_group"]
    path: ContainerPath
    group: AccountName


class ProcessMatch(_Strict):
    comm: Text | None = None
    args_regex: Text | None = None

    @model_validator(mode="after")
    def _one_match(self) -> Self:
        _exactly_one(self, ("comm", "args_regex"))
        return self


class ProcessRunning(_Condition):
    type: Literal["process_running"]
    match: ProcessMatch
    user: AccountName | None = None
    min_count: int = Field(default=1, ge=1, le=128)


class ProcessNotRunning(_Condition):
    type: Literal["process_not_running"]
    match: ProcessMatch
    user: AccountName | None = None


class Answer(_Condition):
    type: Literal["answer"]
    equals: Text | None = None
    equals_param: ParamName | None = None
    one_of: list[Text] | None = Field(default=None, min_length=1)
    normalize: list[Literal["trim", "lowercase"]] = []

    @model_validator(mode="after")
    def _one_expected(self) -> Self:
        _exactly_one(self, ("equals", "equals_param", "one_of"))
        if len(set(self.normalize)) != len(self.normalize):
            raise ValueError("normalize: an option is listed more than once")
        return self


Check = Annotated[
    FileExists
    | DirectoryExists
    | PathAbsent
    | FileContent
    | FilePermissions
    | FileOwner
    | FileGroup
    | ProcessRunning
    | ProcessNotRunning
    | Answer,
    Field(discriminator="type"),
]


class AllOf(_Strict):
    all: list["Condition"] = Field(min_length=1)
    fail_message: Text | None = None


class AnyOf(_Strict):
    any: list["Condition"] = Field(min_length=1)
    fail_message: Text | None = None


class NotOf(_Strict):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, populate_by_name=True)

    not_: "Condition" = Field(alias="not")
    fail_message: Text


OPERATORS = ("all", "any", "not")


def _condition_kind(value: Any) -> str | None:
    if isinstance(value, BaseModel):
        return "check" if isinstance(value, _Condition) else _operator_of(value)
    if not isinstance(value, dict):
        return None
    keys = [key for key in ("type", *OPERATORS) if key in value]
    if len(keys) != 1:
        return None
    return "check" if keys[0] == "type" else keys[0]


def _operator_of(node: BaseModel) -> str:
    return {AllOf: "all", AnyOf: "any", NotOf: "not"}[type(node)]


Condition = Annotated[
    Annotated[Check, Tag("check")]
    | Annotated[AllOf, Tag("all")]
    | Annotated[AnyOf, Tag("any")]
    | Annotated[NotOf, Tag("not")],
    Discriminator(
        _condition_kind,
        custom_error_type="invalid_condition",
        custom_error_message="a condition needs either a type or exactly one of all, any, not",
    ),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
NotOf.model_rebuild()


def children(node: BaseModel) -> list[BaseModel]:
    if isinstance(node, AllOf):
        return list(node.all)
    if isinstance(node, AnyOf):
        return list(node.any)
    if isinstance(node, NotOf):
        return [node.not_]
    return []


def checks(node: BaseModel) -> Iterator[_Condition]:
    """The leaves of a condition tree, in order."""
    if isinstance(node, _Condition):
        yield node
    for child in children(node):
        yield from checks(child)


def depth(node: BaseModel) -> int:
    return 1 + max((depth(child) for child in children(node)), default=0)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


# Parameters


class HexParam(_Strict):
    generator: Literal["hex"]
    length: int = Field(ge=1, le=64)


class WordParam(_Strict):
    generator: Literal["word"]


class IntParam(_Strict):
    generator: Literal["int"]
    min: int = Field(ge=-(10**18), le=10**18)
    max: int = Field(ge=-(10**18), le=10**18)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError("min must not be greater than max")
        return self


class ChoiceParam(_Strict):
    generator: Literal["choice"]
    values: list[ParamValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_values(self) -> Self:
        if len(set(self.values)) != len(self.values):
            raise ValueError("values: a value is listed more than once")
        return self


Param = Annotated[HexParam | WordParam | IntParam | ChoiceParam, Field(discriminator="generator")]


# Mission file


class Environment(_Strict):
    image: ImageAlias
    profile: Literal["default"]


# The identity setup runs as inside the lab.
SetupUser = Literal["student", "root"]


class Setup(_Strict):
    script: FilePath
    user: SetupUser = "student"
    timeout_seconds: int = Field(ge=1, le=60)


class Script(_Strict):
    label: Text
    script: FilePath


class MissionFile(_Strict):
    schema_: SchemaVersion = Field(alias="schema")
    slug: Slug
    title: Text
    summary: Text
    difficulty: int = Field(ge=1, le=5)
    estimated_minutes: int = Field(ge=1)
    status: Status
    tags: list[Slug] = []

    briefing: FilePath
    explanation: FilePath
    objectives: list[Text] = Field(min_length=1)

    environment: Environment
    params: dict[ParamName, Param] = {}
    setup: Setup
    hints: list[Text] = []
    validation: Condition

    solutions: list[Script] = Field(min_length=2)
    counterexamples: list[Script] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("tags: a tag is listed more than once")
        if len({solution.script for solution in self.solutions}) != len(self.solutions):
            raise ValueError("solutions: each solution must use a different script")
        self._check_conditions()
        return self

    def _check_conditions(self) -> None:
        leaves = list(checks(self.validation))
        if len(leaves) > MAX_CONDITIONS:
            raise ValueError(f"validation: more than {MAX_CONDITIONS} conditions")
        if depth(self.validation) > MAX_CONDITION_DEPTH:
            raise ValueError(f"validation: nested deeper than {MAX_CONDITION_DEPTH} levels")
        ids = [leaf.id for leaf in leaves]
        repeated = sorted({leaf_id for leaf_id in ids if ids.count(leaf_id) > 1})
        if repeated:
            raise ValueError(f"validation: condition id used more than once: {repeated[0]}")
        for leaf in leaves:
            if isinstance(leaf, Answer) and leaf.equals_param not in (None, *self.params):
                raise ValueError(
                    f"validation: condition {leaf.id}: unknown parameter {leaf.equals_param}"
                )
        for text in _strings(self.validation.model_dump(by_alias=True)):
            self._check_placeholders(text)

    def _check_placeholders(self, text: str) -> None:
        """`{{ name }}` is a plain lookup of a declared parameter; nothing else is allowed."""
        if "{{" not in text and "}}" not in text:
            return
        rest = PLACEHOLDER.sub("", text)
        if "{{" in rest or "}}" in rest:
            raise ValueError("validation: malformed {{ }} placeholder")
        for name in PLACEHOLDER.findall(text):
            if name not in self.params:
                raise ValueError(f"validation: unknown parameter in placeholder: {name}")

    def has_answer(self) -> bool:
        return any(isinstance(leaf, Answer) for leaf in checks(self.validation))
