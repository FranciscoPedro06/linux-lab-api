from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LabSettings(BaseSettings):
    """Settings needed to run labs, without the rest of the application."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lab_oci_runtime: str = "runsc"
    lab_image: str = "linuxlab/lab-base:dev"
    # Written to the linuxlab.deployment label of every lab container. Reconciliation
    # only touches containers with this deployment's label, so deployments sharing a
    # Docker Engine (or the test suite) never remove each other's labs.
    lab_deployment: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class Settings(LabSettings):
    database_url: str

    # Origins allowed to send non-GET API requests and to open a WebSocket,
    # comma-separated in the environment.
    allowed_origins: Annotated[frozenset[str], NoDecode] = frozenset()

    # Invite code required to sign up during the closed beta. Unset or empty
    # disables sign-up.
    signup_invite_code: SecretStr | None = None

    # Enables the terminal WebSocket without authentication, for local development.
    # Anyone who can reach the API and knows a lab id can use that lab.
    dev_terminal_access: bool = False

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return frozenset(origin.strip() for origin in value.split(",") if origin.strip())
        return value

    @field_validator("signup_invite_code", mode="before")
    @classmethod
    def _empty_invite_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
