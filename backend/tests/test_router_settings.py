from __future__ import annotations

from pathlib import Path

from router_service.settings import Settings


def test_router_settings_loads_explicit_env_file(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "router.env"
    env_file.write_text(
        "\n".join(
            (
                "ROUTER_API_ENV=prod",
                "ROUTER_LLM_API_BASE_URL=https://example.test/v1",
                "ROUTER_LLM_API_KEY=test-key",
                "ROUTER_LLM_MODEL=gpt-test",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROUTER_ENV_FILE", str(env_file))
    monkeypatch.delenv("ROUTER_API_ENV", raising=False)
    monkeypatch.delenv("ROUTER_LLM_API_BASE_URL", raising=False)
    monkeypatch.delenv("ROUTER_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ROUTER_LLM_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.env == "prod"
    assert settings.llm_api_base_url == "https://example.test/v1"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_model == "gpt-test"


def test_router_settings_missing_explicit_env_file_is_safe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ROUTER_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.delenv("ROUTER_LLM_API_BASE_URL", raising=False)
    monkeypatch.delenv("ROUTER_LLM_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.llm_api_base_url is None
    assert settings.llm_model is None


def test_router_settings_support_file_catalog_backend(monkeypatch) -> None:
    monkeypatch.setenv("ROUTER_INTENT_CATALOG_BACKEND", "file")
    monkeypatch.setenv("ROUTER_INTENT_CATALOG_FILE", "/etc/intent-router/catalog.json")
    monkeypatch.setenv("ROUTER_INTENT_FIELD_CATALOG_FILE", "/etc/intent-router/field-catalogs.json")
    monkeypatch.setenv("ROUTER_INTENT_SLOT_SCHEMA_FILE", "/etc/intent-router/slot-schemas.json")
    monkeypatch.setenv("ROUTER_INTENT_GRAPH_BUILD_HINTS_FILE", "/etc/intent-router/graph-build-hints.json")

    settings = Settings.from_env()

    assert settings.repository_backend == "file"
    assert settings.router_intent_catalog_file == "/etc/intent-router/catalog.json"
    assert settings.router_intent_field_catalog_file == "/etc/intent-router/field-catalogs.json"
    assert settings.router_intent_slot_schema_file == "/etc/intent-router/slot-schemas.json"
    assert settings.router_intent_graph_build_hints_file == "/etc/intent-router/graph-build-hints.json"


def test_router_settings_supports_llm_auth_http_client_switch(monkeypatch) -> None:
    monkeypatch.setenv("ROUTER_LLM_AUTH_HTTP_CLIENT_ENABLED", "true")

    settings = Settings.from_env()

    assert settings.llm_auth_http_client_enabled is True
