"""Rules for account fields. Each check returns the value to store, or None."""

import unicodedata

from email_validator import EmailNotValidError, validate_email

MAX_DISPLAY_NAME_LENGTH = 80


def normalize_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> str | None:
    """Normalized email if it is a valid ASCII address, otherwise None."""
    normalized = normalize_email(email)
    try:
        validate_email(normalized, check_deliverability=False, allow_smtputf8=False)
    except EmailNotValidError:
        return None
    return normalized if normalized.isascii() else None


def valid_display_name(display_name: str) -> str | None:
    """Stripped name if it has 1 to 80 characters and no control characters."""
    name = display_name.strip()
    if not 1 <= len(name) <= MAX_DISPLAY_NAME_LENGTH:
        return None
    if any(unicodedata.category(char) == "Cc" for char in name):
        return None
    return name
