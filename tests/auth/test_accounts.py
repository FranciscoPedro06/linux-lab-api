import pytest

from linuxlab.auth.accounts import normalize_email, valid_display_name, valid_email


def test_email_is_stripped_and_lowercased() -> None:
    assert normalize_email("  Ana.Silva@Example.COM \n") == "ana.silva@example.com"
    assert valid_email("  Ana.Silva@Example.COM ") == "ana.silva@example.com"


@pytest.mark.parametrize(
    "email",
    ["", "ana", "ana@", "@example.com", "ana@@example.com", "ana @example.com", "ána@example.com"],
)
def test_invalid_emails(email: str) -> None:
    assert valid_email(email) is None


def test_display_name_is_stripped_and_keeps_case_and_markup() -> None:
    assert valid_display_name("  Ana <b>Silva</b> ") == "Ana <b>Silva</b>"


@pytest.mark.parametrize("name", ["", "   ", "a" * 81, "Ana\x00", "Ana\nSilva"])
def test_invalid_display_names(name: str) -> None:
    assert valid_display_name(name) is None


def test_display_name_length_bounds() -> None:
    assert valid_display_name("a") == "a"
    assert valid_display_name("é" * 80) == "é" * 80
