"""Condition trees for validation unit tests, read like a stored version."""

from typing import Any

from linuxlab.content.stored import validation_spec
from linuxlab.validation.plan import Plan, plan

PARAMS = {"token": "3fa9c01e", "word": "lantern"}
DECLARED = {"token": {"generator": "hex", "length": 8}, "word": {"generator": "word"}}


def stored(validation: dict[str, Any]) -> dict[str, Any]:
    return {"validation": validation, "params": DECLARED}


def make_plan(validation: dict[str, Any], params: dict[str, str] | None = None) -> Plan:
    return plan(validation_spec(stored(validation)).tree, PARAMS if params is None else params)


def leaf(id_: str, type_: str, **fields: Any) -> dict[str, Any]:
    return {"id": id_, "type": type_, **fields}
