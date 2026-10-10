"""Parameter generation, with a scripted source of randomness."""

import builtins
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from linuxlab.content.params import (
    HEX_DIGITS,
    SYSTEM_RANDOMNESS,
    WORDS,
    ParamError,
    generate_params,
)
from linuxlab.content.schema import PARAM_VALUE, ChoiceParam, IntParam, Param, ParamName

DECLARED = TypeAdapter(dict[ParamName, Param])


def declared(**params: dict[str, Any]) -> dict[str, Param]:
    return DECLARED.validate_python(params)


class Scripted:
    """Answers each draw with the next index, wrapped to the size of the draw."""

    def __init__(self, *indexes: int) -> None:
        self._indexes: Iterator[int] = iter(indexes)
        self.draws: list[int] = []

    def choice[T](self, items: Sequence[T]) -> T:
        self.draws.append(len(items))
        return items[next(self._indexes) % len(items)]

    def randbelow(self, bound: int) -> int:
        self.draws.append(bound)
        return next(self._indexes) % bound


class Constant:
    """Always the first item, or the lowest number."""

    def choice[T](self, items: Sequence[T]) -> T:
        return items[0]

    def randbelow(self, bound: int) -> int:
        return 0


def test_hex_has_the_declared_length_in_lowercase() -> None:
    randomness = Scripted(15, 10, 0, 9)
    assert generate_params(declared(token={"generator": "hex", "length": 4}), randomness) == {
        "token": "fa09"
    }
    assert randomness.draws == [16] * 4

    for length in (1, 64):
        value = generate_params(declared(t={"generator": "hex", "length": length}))["t"]
        assert len(value) == length
        assert set(value) <= set(HEX_DIGITS)


def test_int_is_within_the_inclusive_range_as_decimal() -> None:
    params = declared(n={"generator": "int", "min": -5, "max": 5})
    randomness = Scripted(0)
    assert generate_params(params, randomness) == {"n": "-5"}
    assert randomness.draws == [11]
    assert generate_params(params, Scripted(10)) == {"n": "5"}

    extreme = declared(n={"generator": "int", "min": -(10**18), "max": 10**18})
    assert generate_params(extreme, Scripted(0)) == {"n": str(-(10**18))}
    assert generate_params(extreme, Scripted(2 * 10**18)) == {"n": str(10**18)}
    single = declared(n={"generator": "int", "min": 7, "max": 7})
    assert generate_params(single) == {"n": "7"}


def test_choice_picks_one_of_the_declared_values() -> None:
    params = declared(color={"generator": "choice", "values": ["red", "green", "blue"]})
    assert generate_params(params, Scripted(1)) == {"color": "green"}
    for _ in range(20):
        assert generate_params(params)["color"] in ("red", "green", "blue")


def test_word_comes_from_the_project_word_list() -> None:
    params = declared(word={"generator": "word"})
    assert generate_params(params, Scripted(0)) == {"word": WORDS[0]}
    assert generate_params(params, Scripted(len(WORDS) - 1)) == {"word": WORDS[-1]}
    assert generate_params(params)["word"] in WORDS


def test_word_list_is_lowercase_ascii_and_distinct() -> None:
    assert len(WORDS) >= 200
    assert len(set(WORDS)) == len(WORDS)
    assert list(WORDS) == sorted(WORDS)
    for word in WORDS:
        assert word.isascii() and word.isalpha() and word.islower(), word
        assert PARAM_VALUE.fullmatch(word), word


def test_every_declared_parameter_gets_a_string_value() -> None:
    params = declared(
        token={"generator": "hex", "length": 8},
        count={"generator": "int", "min": 1, "max": 3},
        color={"generator": "choice", "values": ["red"]},
        word={"generator": "word"},
    )
    values = generate_params(params, Constant())
    assert values == {"token": "00000000", "count": "1", "color": "red", "word": WORDS[0]}
    assert all(isinstance(value, str) and PARAM_VALUE.fullmatch(value) for value in values.values())
    assert generate_params({}) == {}


def test_system_randomness_uses_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets

    calls: list[str] = []

    def choice(items: Sequence[str]) -> str:
        calls.append("choice")
        return items[0]

    def randbelow(bound: int) -> int:
        calls.append("randbelow")
        return 0

    monkeypatch.setattr(secrets, "choice", choice)
    monkeypatch.setattr(secrets, "randbelow", randbelow)
    assert SYSTEM_RANDOMNESS.choice("ab") == "a"
    assert SYSTEM_RANDOMNESS.randbelow(10) == 0
    assert calls == ["choice", "randbelow"]


def test_a_value_outside_the_format_is_refused_without_quoting_it() -> None:
    class Broken:
        def choice[T](self, items: Sequence[T]) -> T:
            return "SECRET VALUE"  # type: ignore[return-value]

        def randbelow(self, bound: int) -> int:
            return 0

    with pytest.raises(ParamError) as error:
        generate_params(declared(word={"generator": "word"}), Broken())
    assert "word" in str(error.value)
    assert "SECRET" not in str(error.value)


def test_generation_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("I/O during parameter generation")

    monkeypatch.setattr(builtins, "open", forbidden)
    params = declared(
        token={"generator": "hex", "length": 8},
        word={"generator": "word"},
        count={"generator": "int", "min": 0, "max": 9},
    )
    assert set(generate_params(params)) == {"token", "word", "count"}


@pytest.mark.parametrize(
    "param",
    [
        {"generator": "hex", "length": 0},
        {"generator": "hex", "length": 65},
        {"generator": "int", "min": 2, "max": 1},
        {"generator": "int", "min": 0, "max": 10**18 + 1},
        {"generator": "choice", "values": []},
        {"generator": "choice", "values": ["Upper"]},
        {"generator": "choice", "values": ["a", "a"]},
        {"generator": "word", "list": "custom"},
        {"generator": "uuid"},
    ],
)
def test_invalid_declarations_are_refused_by_the_schema(param: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        declared(p=param)


def test_generators_are_the_schema_models() -> None:
    params = declared(
        n={"generator": "int", "min": 0, "max": 1}, c={"generator": "choice", "values": ["x"]}
    )
    assert isinstance(params["n"], IntParam)
    assert isinstance(params["c"], ChoiceParam)
