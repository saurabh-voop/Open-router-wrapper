"""Application settings, loaded from environment variables (SPEC section 10: secrets via env)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-please"  # noqa: S105 (refused in production)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    database_url: str = "postgresql+asyncpg://relay:relay@localhost:5432/relay"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    catalog_sync_enabled: bool = True
    # Refuse to start unless every routing.yaml slug is in the catalog with a ZDR endpoint.
    # Always on in production (TASKS Phase 3).
    routing_strict: bool = False

    jwt_secret: str = Field(default=DEV_JWT_SECRET, min_length=32)
    jwt_expiry_days: int = 7
    fernet_key: str = ""

    aws_region: str = "ap-south-1"
    s3_bucket: str = "relay-files"
    s3_endpoint_url: str | None = None  # set for MinIO in development

    sandbox_url: str = ""  # e.g. http://sandbox:8100; empty disables run_python
    sandbox_token: str = ""

    clamav_host: str = ""
    clamav_port: int = 3310

    default_monthly_cap_usd: float = 50.0
    app_base_url: str = "http://localhost:3000"

    routing_config_path: Path = REPO_ROOT / "config" / "routing.yaml"
    connectors_config_path: Path = REPO_ROOT / "config" / "connectors.yaml"

    @model_validator(mode="after")
    def _production_secrets(self) -> "Settings":
        if self.environment == "production":
            if self.jwt_secret == DEV_JWT_SECRET:
                raise ValueError("JWT_SECRET must be set in production")
            if not self.openrouter_api_key:
                raise ValueError("OPENROUTER_API_KEY must be set in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
