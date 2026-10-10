"""Reading what provisioning needs back from a stored mission version."""

import copy
from typing import Any

import pytest

from linuxlab.content.loader import load_content
from linuxlab.content.schema import HexParam
from linuxlab.content.stored import InvalidSpecError, provisioning_spec

from .support import FIXTURE


def stored(slug: str = "sample-file") -> dict[str, Any]:
    mission = next(m for m in load_content(FIXTURE).missions if m.slug == slug)
    return copy.deepcopy(mission.spec)


def test_reads_environment_params_and_setup_of_the_version() -> None:
    spec = stored()
    result = provisioning_spec(spec)

    assert result.environment.image == "base"
    assert result.environment.profile == "default"
    assert result.params == {"token": HexParam(generator="hex", length=8)}
    script = (FIXTURE / "missions" / "sample-file" / "setup.sh").read_text(encoding="utf-8")
    assert result.setup.script == script
    assert result.setup.user == "student"
    assert result.setup.timeout_seconds == 20


def test_reads_the_root_setup_user() -> None:
    spec = stored()
    spec["setup"]["user"] = "root"
    assert provisioning_spec(spec).setup.user == "root"


def test_mission_without_params() -> None:
    spec = stored("sample-draft")
    spec.pop("params", None)
    assert provisioning_spec(spec).params == {}


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda spec: spec.pop("setup"),
        lambda spec: spec["setup"].pop("script"),
        lambda spec: spec["setup"].update(script="setup.sh"),
        lambda spec: spec["setup"]["script"].update(content=7),
        lambda spec: spec["setup"].update(user="admin"),
        lambda spec: spec["setup"].update(timeout_seconds=600),
        lambda spec: spec["environment"].update(image="other"),
        lambda spec: spec["params"].update(token={"generator": "uuid"}),
        lambda spec: spec["params"].update(Token={"generator": "word"}),
    ],
)
def test_invalid_stored_specs_are_refused(corrupt: Any) -> None:
    spec = stored()
    corrupt(spec)
    with pytest.raises(InvalidSpecError) as error:
        provisioning_spec(spec)
    assert "uuid" not in str(error.value)
    assert error.value.__cause__ is None
