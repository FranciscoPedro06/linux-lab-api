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
