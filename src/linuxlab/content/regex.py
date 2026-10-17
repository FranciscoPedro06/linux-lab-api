"""RE2 patterns of mission conditions (`file_content.regex`, `match.args_regex`).

Patterns use RE2 syntax only: there is no fallback to Python's `re`, so lookaround,
backreferences and other constructs RE2 does not support are refused. RE2 matches
in time linear in the input, and compilation is bounded by MAX_MEM.

The sync compiles every pattern to check its syntax, with each `{{ name }}`
placeholder replaced by a literal. Validation compiles it again with the lab's
parameter values, escaped so they only ever match themselves. Errors never quote the
pattern, which can hold a parameter value once placeholders are replaced, and RE2's
own error log is turned off for the same reason.
"""

import re

import re2

MAX_PATTERN_LENGTH = 1024
# Memory budget for compiling and running one pattern, in bytes. RE2's default is
# 8 MiB; patterns of at most MAX_PATTERN_LENGTH characters fit comfortably in 1 MiB.
MAX_MEM = 1024 * 1024

PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
# Stands in for every placeholder when only the syntax is checked. Parameter values
# are escaped before compilation, so any literal gives the same structure.
SYNTAX_STAND_IN = "a"


class PatternError(ValueError):
    """The pattern is not valid RE2. The message never contains the pattern."""


def _options() -> re2.Options:
    options = re2.Options()
    options.max_mem = MAX_MEM
    options.log_errors = False
    return options


def compile_pattern(pattern: str) -> re2._Regexp:
    """Compile a pattern whose placeholders have already been replaced.

    The length limit applies to the pattern as written (check_syntax): replaced
    placeholders can make it longer.
    """
    try:
        return re2.compile(pattern, _options())
    except re2.error as error:
        reason = error.args[0] if error.args else b""
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", "replace")
        # RE2 reasons name the construct after a colon (`missing ]: [...`); keep only
        # the description before it.
        raise PatternError(f"invalid RE2 regular expression: {str(reason).split(':')[0]}") from None
    except (UnicodeError, TypeError):
        raise PatternError("invalid RE2 regular expression") from None


def check_syntax(pattern: str) -> None:
    """Compile a pattern as written in a mission, placeholders replaced by a literal."""
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError(f"regular expression longer than {MAX_PATTERN_LENGTH} characters")
    compile_pattern(PLACEHOLDER.sub(SYNTAX_STAND_IN, pattern))


def escape(value: str) -> str:
    """A parameter value as a pattern that matches exactly that value."""
    return re2.escape(value)
