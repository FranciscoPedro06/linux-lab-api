from functools import lru_cache
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LabSettings(BaseSettings):
    """Settings needed to run labs, without the rest of the application."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lab_oci_runtime: str = "runsc"
    lab_image: str = "linuxlab/lab-base:dev"


class Settings(LabSettings):
    database_url: str

    # Origins allowed to open a WebSocket, comma-separated in the environment.
    allowed_origins: Annotated[frozenset[str], NoDecode] = frozenset()

    # Enables the terminal WebSocket without authentication, for local development.
    # Anyone who can reach the API and knows a lab id can use that lab.
    dev_terminal_access: bool = False

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return frozenset(origin.strip() for origin in value.split(",") if origin.strip())
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
