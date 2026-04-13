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
