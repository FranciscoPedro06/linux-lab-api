"""What lab provisioning reads from a stored mission version.

mission_versions.spec was produced by the sync from a validated mission.yaml, with
referenced files inlined. Provisioning reads the environment, parameters and setup
back through the same schema models, so a version that no longer fits them (edited
outside the sync, or written by a future format) is refused instead of half used.
Errors never quote the stored values.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from linuxlab.content.schema import Environment, Param, ParamName, Setup, SetupUser


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
