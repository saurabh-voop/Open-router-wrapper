"""Application settings, loaded from environment variables (SPEC section 10: secrets via env)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"

    database_url: str = "postgresql+asyncpg://relay:relay@localhost:5432/relay"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    jwt_secret: str = Field(default="dev-insecure-change-me", min_length=16)
    jwt_expiry_days: int = 7
    fernet_key: str = ""

    aws_region: str = "ap-south-1"
    s3_bucket: str = "relay-files"
    s3_endpoint_url: str | None = None  # set for MinIO in development

    default_monthly_cap_usd: float = 50.0
    app_base_url: str = "http://localhost:3000"

    routing_config_path: Path = REPO_ROOT / "config" / "routing.yaml"
    connectors_config_path: Path = REPO_ROOT / "config" / "connectors.yaml"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
