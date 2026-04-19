from __future__ import annotations

from admin_service.settings import Settings  # noqa: E402


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
    monkeypatch.setenv(
        "ADMIN_PERF_TEST_TARGET_BASE_URL",
        "http://router-api-test.intent.svc.cluster.local:8000",
    )
    monkeypatch.setenv("ADMIN_PERF_TEST_SESSION_CREATE_PATH", "/api/router/v2/sessions")
    monkeypatch.setenv(
        "ADMIN_PERF_TEST_MESSAGE_PATH_TEMPLATE",
        "/api/router/v2/sessions/{session_id}/messages",
    )
    monkeypatch.setenv("ADMIN_PERF_TEST_REQUEST_TIMEOUT_SECONDS", "18")

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
    assert settings.perf_test_target_base_url == "http://router-api-test.intent.svc.cluster.local:8000"
    assert settings.perf_test_session_create_path == "/api/router/v2/sessions"
    assert settings.perf_test_message_path_template == "/api/router/v2/sessions/{session_id}/messages"
    assert settings.perf_test_request_timeout_seconds == 18.0
