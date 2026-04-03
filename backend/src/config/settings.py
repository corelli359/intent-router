from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


TRUE_VALUES = {"1", "true", "yes", "on"}
ENV_FILENAMES = (".env", ".env.local")


def _load_local_env_files() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    for filename in ENV_FILENAMES:
        env_path = repo_root / filename
        if not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = raw_value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _env_headers(name: str) -> dict[str, str]:
    raw_value = os.getenv(name)
    if not raw_value:
        return {}
    parsed = json.loads(raw_value)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return {str(key): str(value) for key, value in parsed.items()}


class Settings(BaseModel):
    app_name: str = Field(default="Intent Router Admin API")
    env: str = Field(default="dev")
    repository_backend: Literal["memory", "postgres"] = Field(default="memory")
    postgres_dsn: str | None = Field(default=None)
    recognizer_backend: Literal["rules", "llm"] = Field(default="rules")
    enable_llm_for_mock_agent: bool = Field(default=False)
    llm_api_base_url: str | None = Field(default=None)
    llm_api_key: str | None = Field(default=None)
    llm_model: str | None = Field(default=None)
    llm_recognizer_model: str | None = Field(default=None)
    llm_agent_model: str | None = Field(default=None)
    llm_structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = Field(
        default="json_mode"
    )
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    agent_http_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_headers: dict[str, str] = Field(default_factory=dict)

    @property
    def default_llm_model(self) -> str | None:
        return self.llm_model or self.llm_recognizer_model or self.llm_agent_model

    @property
    def llm_connection_ready(self) -> bool:
        return bool(self.llm_api_base_url and self.default_llm_model)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_local_env_files()
        return cls(
            app_name=os.getenv("ADMIN_API_APP_NAME", "Intent Router Admin API"),
            env=os.getenv("ADMIN_API_ENV", "dev"),
            repository_backend=os.getenv("ADMIN_REPOSITORY_BACKEND", "memory"),
            postgres_dsn=os.getenv("ADMIN_POSTGRES_DSN"),
            recognizer_backend=os.getenv("ROUTER_RECOGNIZER_BACKEND", "rules"),
            enable_llm_for_mock_agent=_env_bool("ROUTER_ENABLE_LLM_FOR_MOCK_AGENT"),
            llm_api_base_url=os.getenv("ROUTER_LLM_API_BASE_URL"),
            llm_api_key=os.getenv("ROUTER_LLM_API_KEY"),
            llm_model=os.getenv("ROUTER_LLM_MODEL"),
            llm_recognizer_model=os.getenv("ROUTER_LLM_RECOGNIZER_MODEL"),
            llm_agent_model=os.getenv("ROUTER_LLM_AGENT_MODEL"),
            llm_structured_output_method=os.getenv("ROUTER_LLM_STRUCTURED_OUTPUT_METHOD", "json_mode"),
            llm_timeout_seconds=float(os.getenv("ROUTER_LLM_TIMEOUT_SECONDS", "30")),
            agent_http_timeout_seconds=float(os.getenv("ROUTER_AGENT_HTTP_TIMEOUT_SECONDS", "60")),
            llm_headers=_env_headers("ROUTER_LLM_HEADERS_JSON"),
        )
