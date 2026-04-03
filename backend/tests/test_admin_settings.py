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
    monkeypatch.setenv("ADMIN_REPOSITORY_BACKEND", "memory")
    monkeypatch.setenv("ROUTER_RECOGNIZER_BACKEND", "llm")
    monkeypatch.setenv("ROUTER_ENABLE_LLM_FOR_MOCK_AGENT", "1")
    monkeypatch.setenv("ROUTER_LLM_API_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("ROUTER_LLM_MODEL", "router-model")
    monkeypatch.setenv("ROUTER_LLM_HEADERS_JSON", '{"x-test-header":"abc"}')

    settings = Settings.from_env()

    assert settings.app_name == "Admin API Test"
    assert settings.env == "test"
    assert settings.repository_backend == "memory"
    assert settings.postgres_dsn is None
    assert settings.recognizer_backend == "llm"
    assert settings.enable_llm_for_mock_agent is True
    assert settings.llm_api_base_url == "https://llm.example.com/v1"
    assert settings.default_llm_model == "router-model"
    assert settings.llm_headers == {"x-test-header": "abc"}
