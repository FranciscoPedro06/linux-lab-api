"""RE2 patterns: the behavior of the installed google-re2 binding, and our wrapper."""

import logging

import pytest
import re2

from linuxlab.content.regex import (
    MAX_MEM,
    MAX_PATTERN_LENGTH,
    PatternError,
    check_syntax,
    compile_pattern,
    escape,
)


@pytest.mark.parametrize(
    "pattern",
    [r"^TOKEN=[0-9a-f]{8}$", r"(?i)backup", r"\bsleep\s+\d+", r"\p{L}+", r"a|b|c", r"[^/]+\.txt"],
)
def test_valid_patterns_compile(pattern: str) -> None:
    compile_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        r"a(?=b)",  # lookahead
        r"a(?!b)",  # negative lookahead
        r"(?<=a)b",  # lookbehind
        r"(a)\1",  # backreference
        r"(?P<n>a)(?P=n)",  # named backreference
        r"a++",  # possessive quantifier
        r"(?>a)",  # atomic group
        r"\Z",
        r"[",
        r"(",
        r"a{1001}",  # repetition above RE2's limit
    ],
)
def test_unsupported_or_invalid_patterns_are_refused(pattern: str) -> None:
    with pytest.raises(PatternError) as error:
        compile_pattern(pattern)
    # The message never quotes the pattern, which can hold a parameter value.
    assert pattern not in str(error.value)


def test_the_binding_raises_its_own_error_and_has_no_python_fallback() -> None:
    options = re2.Options()
    options.log_errors = False
    with pytest.raises(re2.error):
        re2.compile(r"a(?=b)", options)


def test_compile_errors_are_not_logged(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(PatternError):
        compile_pattern(r"secret(?=x)")
    assert "secret" not in caplog.text
    assert "secret" not in capfd.readouterr().err


def test_memory_limit_applies() -> None:
    assert MAX_MEM == 1024 * 1024
    # Fits in 1 MiB.
    compile_pattern(r"[a-z]{1000}")
    options = re2.Options()
    options.max_mem = 4096
    options.log_errors = False
    with pytest.raises(re2.error):
        re2.compile(r"[a-z]{1000}", options)


def test_length_limit_applies_to_the_pattern_as_written() -> None:
    check_syntax("a" * MAX_PATTERN_LENGTH)
    with pytest.raises(PatternError, match="longer than"):
        check_syntax("a" * (MAX_PATTERN_LENGTH + 1))


def test_syntax_check_replaces_placeholders() -> None:
    check_syntax(r"^TOKEN={{ token }}$")
    check_syntax(r"[{{ token }}]+")
    with pytest.raises(PatternError):
        check_syntax(r"{{ token }}(?=x)")


def test_escaped_values_match_only_themselves() -> None:
    pattern = compile_pattern(f"^{escape('a-b_c')}$")
    assert pattern.search("a-b_c") is not None
    assert pattern.search("aXb_c") is None
    inside_class = compile_pattern(f"^[{escape('a-c')}]+$")
    assert inside_class.search("b") is None
    assert inside_class.search("a-c") is not None


def test_search_is_unanchored() -> None:
    assert compile_pattern("TOKEN").search("export TOKEN=1") is not None
