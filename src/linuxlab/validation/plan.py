"""From a pinned condition tree and a lab's parameters to what the probe must collect.

Placeholders are replaced in memory only, by a plain lookup in the lab's parameters.
In RE2 patterns the value is escaped, so it matches only itself. Nothing here
reaches the client, a log or the database: the resolved tree holds parameter values.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import re2
from pydantic import BaseModel

from linuxlab.content.regex import PLACEHOLDER, PatternError, compile_pattern, escape
from linuxlab.content.schema import (
    PARAM_VALUE,
    AllOf,
    Answer,
    AnyOf,
    DirectoryExists,
    FileContent,
    FileExists,
    FileGroup,
    FileOwner,
    FilePermissions,
    NotOf,
    PathAbsent,
    ProcessMatch,
    ProcessNotRunning,
    ProcessRunning,
    checks,
)
from linuxlab.content.stored import InvalidSpecError
from linuxlab.validation.probe import MAX_FACTS, Fact, ProcessesFact, ReadFact, StatFact

PathCheck = FileExists | DirectoryExists | PathAbsent | FilePermissions | FileOwner | FileGroup


@dataclass(frozen=True)
class Plan:
    # The condition tree with placeholders replaced. Holds parameter values.
    tree: BaseModel
    # Compiled patterns, by condition id.
    patterns: Mapping[str, re2._Regexp]
    # What the probe must collect, without duplicates, in tree order. Empty when every
    # condition is an answer.
    facts: tuple[Fact, ...]


def _invalid() -> InvalidSpecError:
    return InvalidSpecError("condition tree cannot be resolved")


class _Resolver:
    def __init__(self, params: Mapping[str, str]) -> None:
        self._params = params

    def value(self, name: str) -> str:
        try:
            return self._params[name]
        except KeyError:
            raise _invalid() from None

    def text(self, text: str | None) -> str | None:
        if text is None:
            return None
        return PLACEHOLDER.sub(lambda match: self.value(match.group(1)), text)

    def pattern(self, text: str | None) -> str | None:
        if text is None:
            return None
        return PLACEHOLDER.sub(lambda match: escape(self.value(match.group(1))), text)

    def path(self, path: str) -> str:
        resolved = self.text(path)
        assert resolved is not None
        # Parameter values cannot form `.`, `..`, `/` or an empty component; checked
        # anyway, since labctl refuses a non-canonical path outright.
        if any(part in ("", ".", "..") for part in resolved[1:].split("/")):
            raise _invalid()
        return resolved


def _resolve(node: BaseModel, resolver: _Resolver) -> BaseModel:
    # model_copy does not validate again: limits apply to the mission as written, and
    # a replaced placeholder can make a value longer.
    if isinstance(node, AllOf):
        return node.model_copy(update={"all": [_resolve(child, resolver) for child in node.all]})
    if isinstance(node, AnyOf):
        return node.model_copy(update={"any": [_resolve(child, resolver) for child in node.any]})
    if isinstance(node, NotOf):
        return node.model_copy(update={"not_": _resolve(node.not_, resolver)})
    if isinstance(node, FileContent):
        return node.model_copy(
            update={
                "path": resolver.path(node.path),
                "equals": resolver.text(node.equals),
                "contains": resolver.text(node.contains),
                "not_contains": resolver.text(node.not_contains),
                "regex": resolver.pattern(node.regex),
            }
        )
    if isinstance(node, PathCheck):
        return node.model_copy(update={"path": resolver.path(node.path)})
    if isinstance(node, ProcessRunning | ProcessNotRunning):
        match = ProcessMatch.model_construct(
            comm=resolver.text(node.match.comm), args_regex=resolver.pattern(node.match.args_regex)
        )
        return node.model_copy(update={"match": match})
    if isinstance(node, Answer):
        one_of = None if node.one_of is None else [resolver.text(item) for item in node.one_of]
        # equals_param becomes the lab's value, compared like equals.
        equals = (
            resolver.value(node.equals_param)
            if node.equals_param is not None
            else resolver.text(node.equals)
        )
        return node.model_copy(update={"equals": equals, "equals_param": None, "one_of": one_of})
    raise _invalid()


def fact_for(check: BaseModel) -> Fact | None:
    if isinstance(check, FileExists | DirectoryExists):
        return StatFact(check.path, follow=check.follow_symlinks)
    if isinstance(check, PathAbsent | FilePermissions | FileOwner | FileGroup):
        return StatFact(check.path, follow=False)
    if isinstance(check, FileContent):
        return ReadFact(check.path)
    if isinstance(check, ProcessRunning | ProcessNotRunning):
        return ProcessesFact()
    return None


def pattern_of(check: BaseModel) -> str | None:
    if isinstance(check, FileContent):
        return check.regex
    if isinstance(check, ProcessRunning | ProcessNotRunning):
        return check.match.args_regex
    return None


def plan(tree: BaseModel, params: Mapping[str, str]) -> Plan:
    """Resolve the tree with the lab's parameters. Raises InvalidSpecError when a
    parameter is missing or invalid, or a resolved pattern does not compile."""
    if not all(
        isinstance(value, str) and PARAM_VALUE.fullmatch(value) for value in params.values()
    ):
        raise _invalid()
    resolved = _resolve(tree, _Resolver(params))
    patterns: dict[str, re2._Regexp] = {}
    facts: dict[Fact, None] = {}
    for check in checks(resolved):
        pattern = pattern_of(check)
        if pattern is not None:
            try:
                patterns[check.id] = compile_pattern(pattern)
            except PatternError:
                raise _invalid() from None
        fact = fact_for(check)
        if fact is not None:
            facts[fact] = None
    if len(facts) > MAX_FACTS:
        raise _invalid()
    return Plan(tree=resolved, patterns=patterns, facts=tuple(facts))
