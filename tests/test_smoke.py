"""Smoke tests proving the skeleton imports and the API boots."""

from fastapi.testclient import TestClient

from leaksentinel import __version__
from leaksentinel.api.main import app
from leaksentinel.config import LLMProvider, get_settings

client = TestClient(app)


def test_version_present() -> None:
    assert __version__ == "0.1.0"


def test_settings_defaults() -> None:
    settings = get_settings()
    assert settings.llm_provider in (LLMProvider.ANTHROPIC, LLMProvider.GROQ)
    assert settings.active_llm_model


def test_health_endpoint() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_root_endpoint() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "leaksentinel"
