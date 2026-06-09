"""Application configuration loaded from the environment / `.env`.

Centralizes settings so the rest of the codebase never reads ``os.environ``
directly. Supports two LLM providers: Anthropic (default) and Groq.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    GROQ = "groq"


class Settings(BaseSettings):
    """Typed application settings.

    Values are read from environment variables (case-insensitive) or a local
    ``.env`` file. See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://leaksentinel:leaksentinel@localhost:5433/leaksentinel",
    )

    # LLM selection
    llm_provider: LLMProvider = LLMProvider.ANTHROPIC

    # Anthropic
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-8"

    # Groq
    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"

    # Observability (LangSmith)
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "leaksentinel"

    @property
    def active_llm_model(self) -> str:
        """Return the model name for the currently selected provider."""
        if self.llm_provider is LLMProvider.GROQ:
            return self.groq_model
        return self.anthropic_model


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()
