from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


ENV_FILENAMES = (".env", ".env.local")


def _env_search_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()

    for candidate in (Path.cwd(), Path("/workspace")):
        resolved = candidate.expanduser().resolve()
        if not resolved.exists() or resolved in seen:
            continue
        roots.append(resolved)
        seen.add(resolved)

    for parent in Path(__file__).resolve().parents:
        if parent in seen:
            continue
        roots.append(parent)
        seen.add(parent)
        if (parent / ".git").exists() or (parent / "AGENTS.md").is_file():
            break
    return tuple(roots)


def _load_local_env_files() -> None:
    for root in _env_search_roots():
        for filename in ENV_FILENAMES:
            env_path = root / filename
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
    repository_backend: Literal["memory", "database", "postgres"] = Field(default="memory")
    database_url: str | None = Field(default=None)
    recognizer_backend: Literal["rules", "llm"] = Field(default="llm")
    router_v2_graph_build_mode: Literal["legacy", "unified"] = Field(default="legacy")
    router_intent_refresh_interval_seconds: float = Field(default=5.0, gt=0)
    router_intent_switch_threshold: float = Field(default=0.80, ge=0, le=1)
    router_agent_timeout_seconds: float = Field(default=60.0, gt=0)
    router_sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    router_sse_max_idle_seconds: float = Field(default=300.0, gt=0)
    llm_api_base_url: str | None = Field(default=None)
    llm_api_key: str | None = Field(default=None)
    llm_model: str | None = Field(default=None)
    llm_recognizer_model: str | None = Field(default=None)
    llm_recognizer_system_prompt_template: str | None = Field(default=None)
    llm_recognizer_human_prompt_template: str | None = Field(default=None)
    llm_structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = Field(
        default="json_mode"
    )
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_rate_limit_max_retries: int = Field(default=2, ge=0)
    llm_rate_limit_retry_delay_seconds: float = Field(default=2.0, gt=0)
    agent_http_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_headers: dict[str, str] = Field(default_factory=dict)
    perf_test_target_base_url: str = Field(
        default="http://router-api-test.intent.svc.cluster.local:8000",
        min_length=1,
        max_length=2048,
    )
    perf_test_session_create_path: str = Field(
        default="/api/router/v2/sessions",
        min_length=1,
        max_length=512,
    )
    perf_test_message_path_template: str = Field(
        default="/api/router/v2/sessions/{session_id}/messages",
        min_length=1,
        max_length=1024,
    )
    perf_test_request_timeout_seconds: float = Field(default=15.0, gt=0)

    @property
    def default_llm_model(self) -> str | None:
        return self.llm_recognizer_model or self.llm_model

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
            database_url=os.getenv("ADMIN_DATABASE_URL") or os.getenv("ADMIN_POSTGRES_DSN"),
            recognizer_backend=os.getenv("ROUTER_RECOGNIZER_BACKEND", "llm"),
            router_v2_graph_build_mode=os.getenv("ROUTER_V2_GRAPH_BUILD_MODE", "legacy"),
            router_intent_refresh_interval_seconds=float(
                os.getenv("ROUTER_INTENT_REFRESH_INTERVAL_SECONDS", "5")
            ),
            router_intent_switch_threshold=float(os.getenv("ROUTER_INTENT_SWITCH_THRESHOLD", "0.8")),
            router_agent_timeout_seconds=float(os.getenv("ROUTER_AGENT_TIMEOUT_SECONDS", "60")),
            router_sse_heartbeat_seconds=float(os.getenv("ROUTER_SSE_HEARTBEAT_SECONDS", "15")),
            router_sse_max_idle_seconds=float(os.getenv("ROUTER_SSE_MAX_IDLE_SECONDS", "300")),
            llm_api_base_url=os.getenv("ROUTER_LLM_API_BASE_URL"),
            llm_api_key=os.getenv("ROUTER_LLM_API_KEY"),
            llm_model=os.getenv("ROUTER_LLM_MODEL"),
            llm_recognizer_model=os.getenv("ROUTER_LLM_RECOGNIZER_MODEL"),
            llm_recognizer_system_prompt_template=os.getenv("ROUTER_LLM_RECOGNIZER_SYSTEM_PROMPT_TEMPLATE"),
            llm_recognizer_human_prompt_template=os.getenv("ROUTER_LLM_RECOGNIZER_HUMAN_PROMPT_TEMPLATE"),
            llm_structured_output_method=os.getenv("ROUTER_LLM_STRUCTURED_OUTPUT_METHOD", "json_mode"),
            llm_timeout_seconds=float(os.getenv("ROUTER_LLM_TIMEOUT_SECONDS", "30")),
            llm_rate_limit_max_retries=int(os.getenv("ROUTER_LLM_RATE_LIMIT_MAX_RETRIES", "2")),
            llm_rate_limit_retry_delay_seconds=float(
                os.getenv("ROUTER_LLM_RATE_LIMIT_RETRY_DELAY_SECONDS", "2")
            ),
            agent_http_timeout_seconds=float(os.getenv("ROUTER_AGENT_HTTP_TIMEOUT_SECONDS", "60")),
            llm_headers=_env_headers("ROUTER_LLM_HEADERS_JSON"),
            perf_test_target_base_url=os.getenv(
                "ADMIN_PERF_TEST_TARGET_BASE_URL",
                "http://router-api-test.intent.svc.cluster.local:8000",
            ),
            perf_test_session_create_path=os.getenv(
                "ADMIN_PERF_TEST_SESSION_CREATE_PATH",
                "/api/router/v2/sessions",
            ),
            perf_test_message_path_template=os.getenv(
                "ADMIN_PERF_TEST_MESSAGE_PATH_TEMPLATE",
                "/api/router/v2/sessions/{session_id}/messages",
            ),
            perf_test_request_timeout_seconds=float(
                os.getenv("ADMIN_PERF_TEST_REQUEST_TIMEOUT_SECONDS", "15")
            ),
        )
