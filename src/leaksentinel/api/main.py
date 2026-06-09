"""FastAPI application entrypoint (skeleton).

Exposes a health check and basic metadata. Business routes will be mounted
under their respective modules as they are implemented.
"""

from __future__ import annotations

from fastapi import FastAPI

from leaksentinel import __version__
from leaksentinel.config import get_settings

app = FastAPI(
    title="LeakSentinel",
    description="Agentic commission-reconciliation engine for two-wheeler insurance distribution.",
    version=__version__,
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok", "version": __version__}


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    """Service metadata."""
    settings = get_settings()
    return {
        "service": "leaksentinel",
        "version": __version__,
        "llm_provider": settings.llm_provider.value,
    }


def run() -> None:
    """Console-script entrypoint: ``leaksentinel``."""
    import uvicorn

    uvicorn.run("leaksentinel.api.main:app", host="127.0.0.1", port=8000, reload=True)
