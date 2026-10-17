"""What lab provisioning and validation read from a stored mission version.

mission_versions.spec was produced by the sync from a validated mission.yaml, with
referenced files inlined. Provisioning reads the environment, parameters and setup,
and validation the condition tree, back through the same schema models and rules,
so a version that no longer fits them (edited
outside the sync, or written by a future format) is refused instead of half used.
Errors never quote the stored values.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from linuxlab.content.schema import (
    Condition,
    Environment,
    Param,
    ParamName,
    Setup,
    SetupUser,
    check_condition_tree,
)


class InvalidSpecError(Exception):
    """The stored specification cannot be used to provision a lab."""


@dataclass(frozen=True)
class SetupScript:
    script: str
    user: SetupUser
    timeout_seconds: int


@dataclass(frozen=True)
class ProvisioningSpec:
    environment: Environment
    params: dict[str, Param]
    setup: SetupScript


_PARAMS = TypeAdapter(dict[ParamName, Param])


def provisioning_spec(spec: Mapping[str, Any]) -> ProvisioningSpec:
    try:
        environment = Environment.model_validate(spec["environment"])
        params = _PARAMS.validate_python(spec.get("params", {}), strict=True)
        stored_setup = spec["setup"]
        script = stored_setup["script"]["content"]
        setup = Setup.model_validate({**stored_setup, "script": stored_setup["script"]["path"]})
    except (KeyError, TypeError, ValidationError):
        raise InvalidSpecError("stored mission specification is invalid") from None
    if not isinstance(script, str):
        raise InvalidSpecError("stored setup script is not text")
    return ProvisioningSpec(
        environment=environment,
        params=params,
        setup=SetupScript(script=script, user=setup.user, timeout_seconds=setup.timeout_seconds),
    )


@dataclass(frozen=True)
class ValidationSpec:
    # The root node of the condition tree, as the schema models.
    tree: BaseModel
    params: frozenset[str]


_CONDITION: TypeAdapter[Any] = TypeAdapter(Condition)


def validation_spec(spec: Mapping[str, Any]) -> ValidationSpec:
    """The condition tree of a stored version, checked again with the rules the sync
    applied (structure, RE2 syntax, canonical paths, limits, parameters)."""
    try:
        params = _PARAMS.validate_python(spec.get("params", {}), strict=True)
        tree = _CONDITION.validate_python(spec["validation"])
        check_condition_tree(tree, params)
    except (KeyError, TypeError, ValueError):
        raise InvalidSpecError("stored mission specification is invalid") from None
    if not isinstance(tree, BaseModel):
        raise InvalidSpecError("stored mission specification is invalid")
    return ValidationSpec(tree=tree, params=frozenset(params))
