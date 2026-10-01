from functools import lru_cache
from typing import Annotated, Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The only OCI runtime production may run labs under (docs/threat-model.md).
PRODUCTION_OCI_RUNTIME = "runsc"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # production refuses to start unless labs run under gVisor: LAB_OCI_RUNTIME must
    # be runsc and Docker must have it registered (checked at startup). Anything
    # that is not explicitly development is production.
    environment: Literal["development", "production"] = "production"

    database_url: str

    # Origins allowed to send non-GET API requests and to open a WebSocket,
    # comma-separated in the environment.
    allowed_origins: Annotated[frozenset[str], NoDecode] = frozenset()

    # Invite code required to sign up during the closed beta. Unset or empty
    # disables sign-up.
    signup_invite_code: SecretStr | None = None

    lab_oci_runtime: str = PRODUCTION_OCI_RUNTIME
    lab_image: str = "linuxlab/lab-base:dev"
    # Written to the linuxlab.deployment label of every lab container. Reconciliation
    # only touches containers with this deployment's label, so deployments sharing a
    # Docker Engine (or the test suite) never remove each other's labs.
    lab_deployment: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    # Labs in provisioning, ready or terminating across all users. Each lab may use
    # 512 MB of memory and half a CPU.
    lab_capacity: int = Field(default=10, ge=1)

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

    @model_validator(mode="after")
    def _production_uses_gvisor(self) -> Self:
        if self.environment == "production" and self.lab_oci_runtime != PRODUCTION_OCI_RUNTIME:
            raise ValueError(
                f"ENVIRONMENT=production requires LAB_OCI_RUNTIME={PRODUCTION_OCI_RUNTIME}, "
                f"got {self.lab_oci_runtime!r}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
