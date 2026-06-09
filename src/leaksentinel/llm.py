"""LLM provider factory.

Returns a LangChain chat model for the configured provider so the rest of the
codebase (and LangGraph nodes) stay provider-agnostic.
"""

from __future__ import annotations

from typing import Any

from leaksentinel.config import LLMProvider, Settings, get_settings


def get_chat_model(settings: Settings | None = None, **kwargs: Any) -> Any:
    """Construct a LangChain chat model for the active provider.

    Imports are local so that installing only one provider's SDK is enough.
    """
    settings = settings or get_settings()

    if settings.llm_provider is LLMProvider.GROQ:
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=settings.groq_model,
            api_key=settings.groq_api_key,
            **kwargs,
        )

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        **kwargs,
    )
