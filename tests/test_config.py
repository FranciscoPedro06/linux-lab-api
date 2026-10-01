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


def test_environment_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LAB_OCI_RUNTIME", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.lab_oci_runtime == "runsc"


@pytest.mark.parametrize("oci_runtime", ["runc", "io.containerd.runc.v2", "", "RUNSC"])
def test_production_requires_runsc(monkeypatch: pytest.MonkeyPatch, oci_runtime: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    monkeypatch.setenv("LAB_OCI_RUNTIME", oci_runtime)
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    with pytest.raises(ValidationError, match="requires LAB_OCI_RUNTIME=runsc"):
        Settings(_env_file=None)


def test_development_may_use_runc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("LAB_OCI_RUNTIME", "runc")

    assert Settings(_env_file=None).lab_oci_runtime == "runc"


def test_unknown_environment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/app")
    monkeypatch.setenv("ENVIRONMENT", "staging")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
