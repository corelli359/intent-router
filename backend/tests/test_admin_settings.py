from __future__ import annotations

import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from config.settings import Settings  # noqa: E402


def test_settings_from_env_reads_admin_values(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_API_APP_NAME", "Admin API Test")
    monkeypatch.setenv("ADMIN_API_ENV", "test")
    monkeypatch.setenv("ADMIN_REPOSITORY_BACKEND", "database")
    monkeypatch.setenv("ADMIN_DATABASE_URL", "sqlite:////tmp/intent-router-test.db")
    monkeypatch.setenv("ROUTER_RECOGNIZER_BACKEND", "llm")
    monkeypatch.setenv("ROUTER_INTENT_REFRESH_INTERVAL_SECONDS", "9")
    monkeypatch.setenv("ROUTER_LLM_API_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("ROUTER_LLM_MODEL", "router-model")
    monkeypatch.setenv("ROUTER_LLM_HEADERS_JSON", '{"x-test-header":"abc"}')

    settings = Settings.from_env()

    assert settings.app_name == "Admin API Test"
    assert settings.env == "test"
    assert settings.repository_backend == "database"
    assert settings.database_url == "sqlite:////tmp/intent-router-test.db"
    assert settings.recognizer_backend == "llm"
    assert settings.router_intent_refresh_interval_seconds == 9.0
    assert settings.llm_api_base_url == "https://llm.example.com/v1"
    assert settings.default_llm_model == "router-model"
    assert settings.llm_headers == {"x-test-header": "abc"}
