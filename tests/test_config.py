import pytest
from pydantic import ValidationError

from linuxlab.config import Settings


def test_database_url_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://user:pass@db:5432/app"


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invite_code_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    monkeypatch.setenv("SIGNUP_INVITE_CODE", "synthetic-invite")

    settings = Settings(_env_file=None)

    assert settings.signup_invite_code is not None
    assert settings.signup_invite_code.get_secret_value() == "synthetic-invite"
    assert "synthetic-invite" not in repr(settings)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_missing_invite_code_disables_signup(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    if value is None:
        monkeypatch.delenv("SIGNUP_INVITE_CODE", raising=False)
    else:
        monkeypatch.setenv("SIGNUP_INVITE_CODE", value)

    assert Settings(_env_file=None).signup_invite_code is None
