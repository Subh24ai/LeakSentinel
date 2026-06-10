"""LangSmith tracing wiring — opt-in, and graceful when not configured.

If ``LANGSMITH_API_KEY`` is set, we turn on LangChain/LangGraph's built-in
LangSmith tracing by exporting the env vars its client reads, and log that it's
on. If the key is absent we do **nothing but log one line** — the pipeline runs
identically, just untraced. No import of the tracing stack is required for the
graph to work, so a missing key (or a missing langsmith install) can never crash
a run.
"""

from __future__ import annotations

import logging
import os

from leaksentinel.config import Settings, get_settings

logger = logging.getLogger(__name__)


def configure_tracing(settings: Settings | None = None) -> bool:
    """Enable LangSmith tracing if a key is present. Returns True iff enabled."""
    settings = settings or get_settings()

    if not settings.langsmith_api_key:
        logger.info(
            "LangSmith tracing disabled (no LANGSMITH_API_KEY); running untraced."
        )
        return False

    # These are the env vars the LangSmith client / LangChain callbacks read.
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"  # legacy var, still honoured
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.langsmith_project)

    logger.info(
        "LangSmith tracing enabled (project=%r).", settings.langsmith_project
    )
    return True
