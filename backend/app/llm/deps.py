"""Process-wide OpenRouter client, created in the app lifespan."""

from functools import lru_cache

from app.core.config import get_settings
from app.llm.openrouter import OpenRouterClient


@lru_cache
def get_openrouter() -> OpenRouterClient:
    settings = get_settings()
    return OpenRouterClient(
        settings.openrouter_api_key, settings.openrouter_base_url, app_url=settings.app_base_url
    )
