"""Mission parameters: values the API generates for each lab. See docs/missions.md#parameters.

Generation is a pure function of the declared parameters and a source of randomness.
It does no I/O and evaluates nothing from the mission: each generator only picks
characters, a number or an item from a fixed list. Values are independent draws per
lab; two labs may receive the same value.

Values are secrets of the lab. They are stored in lab_sessions.params and passed to
setup as environment variables; they never appear in a response, a log or an error
message, including the messages raised here.
"""

import secrets
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Protocol

from linuxlab.content.schema import (
    PARAM_VALUE,
    ChoiceParam,
    HexParam,
    IntParam,
    Param,
    WordParam,
)

HEX_DIGITS = "0123456789abcdef"
# The project's own list, versioned with the code: never read from the lab or the host.
WORDS: tuple[str, ...] = tuple(
    resources.files(__package__).joinpath("words.txt").read_text(encoding="utf-8").split()
)


class Randomness(Protocol):
    def choice[T](self, items: Sequence[T]) -> T: ...

    def randbelow(self, bound: int) -> int:
        """A uniform integer in [0, bound)."""


class SystemRandomness:
    """The operating system's cryptographic source, through `secrets`."""

    def choice[T](self, items: Sequence[T]) -> T:
        return secrets.choice(items)

    def randbelow(self, bound: int) -> int:
        return secrets.randbelow(bound)


SYSTEM_RANDOMNESS = SystemRandomness()


class ParamError(Exception):
    """A parameter could not be generated. The message never contains a value."""


def generate_params(
    declared: Mapping[str, Param], randomness: Randomness = SYSTEM_RANDOMNESS
) -> dict[str, str]:
    """One value for every declared parameter, each a string in the parameter format."""
    values: dict[str, str] = {}
    for name, param in declared.items():
        value = _generate(param, randomness)
        if not PARAM_VALUE.fullmatch(value):
            raise ParamError(f"parameter {name}: generated value has an invalid format")
        values[name] = value
    return values


def _generate(param: Param, randomness: Randomness) -> str:
    match param:
        case HexParam(length=length):
            return "".join(randomness.choice(HEX_DIGITS) for _ in range(length))
        case IntParam(min=low, max=high):
            return str(low + randomness.randbelow(high - low + 1))
        case ChoiceParam(values=choices):
            return randomness.choice(choices)
        case WordParam():
            return randomness.choice(WORDS)
    raise ParamError(f"unknown generator: {type(param).__name__}")
