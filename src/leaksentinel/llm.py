"""LLM provider factory.

Returns a LangChain chat model for the configured provider so the rest of the
codebase (and LangGraph nodes) stay provider-agnostic.
"""

from __future__ import annotations

import logging
from typing import Any

from leaksentinel.config import LLMProvider, Settings, get_settings

logger = logging.getLogger(__name__)


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


# Groq model-name fragments that indicate multimodal/vision capability.
_GROQ_VISION_HINTS = ("vision", "scout", "maverick", "llama-4")


def _is_vision_capable(provider: LLMProvider, model: str) -> bool:
    """All current Anthropic Claude models are vision-capable; Groq only some."""
    if provider is LLMProvider.ANTHROPIC:
        return True
    name = (model or "").lower()
    return any(hint in name for hint in _GROQ_VISION_HINTS)


def get_vision_chat_model(
    settings: Settings | None = None, **kwargs: Any
) -> tuple[Any, str]:
    """Return ``(model, provider_label)`` for a vision-capable chat model.

    Respects ``LLM_PROVIDER`` when that provider can do vision. If the configured
    provider/model cannot (e.g. Groq's text-only Llama), this falls back to
    Anthropic Claude and logs a clear message. Returns the provider actually
    used so callers can surface it.
    """
    settings = settings or get_settings()

    if _is_vision_capable(settings.llm_provider, settings.active_llm_model):
        return get_chat_model(settings, **kwargs), settings.llm_provider.value

    logger.warning(
        "Configured provider %r model %r is not vision-capable; "
        "falling back to Anthropic Claude %r for document extraction.",
        settings.llm_provider.value,
        settings.active_llm_model,
        settings.anthropic_model,
    )
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic(
        model=settings.anthropic_model,
        api_key=settings.anthropic_api_key,
        **kwargs,
    )
    return model, f"anthropic (fallback from {settings.llm_provider.value})"
