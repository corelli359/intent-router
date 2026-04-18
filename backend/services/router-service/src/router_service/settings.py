from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, Field

from router_service.core.support.agent_barrier import ROUTER_AGENT_BARRIER_ENABLED_ENV
from router_service.core.support.json_codec import json_loads


ROUTER_ENV_FILE_ENV = "ROUTER_ENV_FILE"
DEFAULT_ROUTER_ENV_FILE = "/etc/intent-router/.env.local"
JWT_SALT = 'a358f520-6477-484e-8a48-91899677152a'
X_APP_ID = 'app-test'

def _load_env_file(env_path: str | os.PathLike[str] | None) -> None:
    """Load one explicit env file without scanning parent directories."""
    if env_path is None:
        return
    path = Path(env_path).expanduser()
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
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


def _configured_env_file() -> str:
    """Return the single explicit env file path used by the router runtime."""
    return os.getenv(ROUTER_ENV_FILE_ENV, DEFAULT_ROUTER_ENV_FILE)


def _env_headers(name: str) -> dict[str, str]:
    """Parse a JSON-encoded HTTP header map from an environment variable."""
    raw_value = os.getenv(name)
    if not raw_value:
        return {}
    parsed = json_loads(raw_value)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return {str(key): str(value) for key, value in parsed.items()}


def _parse_bool_env(name: str, default: bool) -> bool:
    """Interpret a boolean configuration from an environment variable."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


ROUTER_LONG_TERM_MEMORY_FACT_LIMIT_ENV = "ROUTER_LONG_TERM_MEMORY_FACT_LIMIT"
DEFAULT_LONG_TERM_MEMORY_FACT_LIMIT = 100


def parse_long_term_memory_fact_limit(
    raw_value: str | None, default: int | None = DEFAULT_LONG_TERM_MEMORY_FACT_LIMIT
) -> int | None:
    """Normalize the long-term memory fact limit configuration."""
    if raw_value is None:
        return default
    normalized = raw_value.strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered in {"none", "null", "unbounded", "unlimited"}:
        return None
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise RuntimeError(
            f"{ROUTER_LONG_TERM_MEMORY_FACT_LIMIT_ENV} must be an integer or one of "
            f"none/null/unbounded, got {raw_value!r}"
        ) from exc
    return parsed if parsed > 0 else None


class Settings(BaseModel):
    """Runtime configuration for the router API and its downstream integrations."""

    app_name: str = Field(default="Intent Router API")
    env: str = Field(default="dev")
    router_log_level: str = Field(default="INFO")
    repository_backend: Literal["memory", "database", "postgres", "file"] = Field(default="memory")
    database_url: str | None = Field(default=None)
    router_intent_catalog_file: str | None = Field(default=None)
    router_intent_field_catalog_file: str | None = Field(default=None)
    router_intent_slot_schema_file: str | None = Field(default=None)
    router_intent_graph_build_hints_file: str | None = Field(default=None)
    recognizer_backend: Literal["rules", "llm"] = Field(default="llm")
    router_v2_graph_build_mode: Literal["legacy", "unified"] = Field(default="legacy")
    router_v2_understanding_mode: Literal["flat", "hierarchical"] = Field(default="flat")
    router_v2_planning_policy: Literal["always", "never", "multi_intent_only", "auto"] = Field(default="auto")
    router_intent_refresh_interval_seconds: float = Field(default=5.0, gt=0)
    router_intent_switch_threshold: float = Field(default=0.80, ge=0, le=1)
    router_agent_timeout_seconds: float = Field(default=60.0, gt=0)
    router_sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    router_sse_max_idle_seconds: float = Field(default=300.0, gt=0)
    router_long_term_memory_fact_limit: int | None = Field(default=DEFAULT_LONG_TERM_MEMORY_FACT_LIMIT)
    router_session_cleanup_enabled: bool = Field(default=True)
    router_session_cleanup_interval_seconds: float = Field(default=60.0, gt=0)
    router_drain_max_iterations: int | None = Field(default=None, gt=0)
    router_drain_iteration_multiplier: int = Field(default=3, gt=0)
    router_drain_iteration_floor: int = Field(default=8, gt=0)
    llm_api_base_url: str | None = Field(default=None)
    llm_api_key: str | None = Field(default=None)
    llm_auth_http_client_enabled: bool = Field(default=False)
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
    router_agent_barrier_enabled: bool = Field(default=False)

    @property
    def default_llm_model(self) -> str | None:
        """Return the default model name for generic router LLM calls."""
        return self.llm_recognizer_model or self.llm_model

    @property
    def llm_connection_ready(self) -> bool:
        """Report whether the minimum LLM connection settings are present."""
        return bool(self.llm_api_base_url and self.default_llm_model)

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from process environment plus one explicit env file."""
        _load_env_file(_configured_env_file())
        return cls(
            app_name=os.getenv("ROUTER_API_APP_NAME", "Intent Router API"),
            env=os.getenv("ROUTER_API_ENV", "dev"),
            router_log_level=os.getenv("ROUTER_LOG_LEVEL", "INFO"),
            repository_backend=os.getenv("ROUTER_INTENT_CATALOG_BACKEND", os.getenv("ADMIN_REPOSITORY_BACKEND", "memory")),
            database_url=os.getenv("ROUTER_INTENT_CATALOG_DATABASE_URL")
            or os.getenv("ADMIN_DATABASE_URL")
            or os.getenv("ADMIN_POSTGRES_DSN"),
            router_intent_catalog_file=os.getenv("ROUTER_INTENT_CATALOG_FILE"),
            router_intent_field_catalog_file=os.getenv("ROUTER_INTENT_FIELD_CATALOG_FILE"),
            router_intent_slot_schema_file=os.getenv("ROUTER_INTENT_SLOT_SCHEMA_FILE"),
            router_intent_graph_build_hints_file=os.getenv("ROUTER_INTENT_GRAPH_BUILD_HINTS_FILE"),
            recognizer_backend=os.getenv("ROUTER_RECOGNIZER_BACKEND", "llm"),
            router_v2_graph_build_mode=os.getenv("ROUTER_V2_GRAPH_BUILD_MODE", "legacy"),
            router_v2_understanding_mode=os.getenv("ROUTER_V2_UNDERSTANDING_MODE", "flat"),
            router_v2_planning_policy=os.getenv("ROUTER_V2_PLANNING_POLICY", "auto"),
            router_intent_refresh_interval_seconds=float(
                os.getenv("ROUTER_INTENT_REFRESH_INTERVAL_SECONDS", "5")
            ),
            router_intent_switch_threshold=float(os.getenv("ROUTER_INTENT_SWITCH_THRESHOLD", "0.8")),
            router_agent_timeout_seconds=float(os.getenv("ROUTER_AGENT_TIMEOUT_SECONDS", "60")),
            router_sse_heartbeat_seconds=float(os.getenv("ROUTER_SSE_HEARTBEAT_SECONDS", "15")),
            router_sse_max_idle_seconds=float(os.getenv("ROUTER_SSE_MAX_IDLE_SECONDS", "300")),
            router_long_term_memory_fact_limit=parse_long_term_memory_fact_limit(
                os.getenv(ROUTER_LONG_TERM_MEMORY_FACT_LIMIT_ENV)
            ),
            router_session_cleanup_enabled=_parse_bool_env("ROUTER_SESSION_CLEANUP_ENABLED", True),
            router_session_cleanup_interval_seconds=float(
                os.getenv("ROUTER_SESSION_CLEANUP_INTERVAL_SECONDS", "60")
            ),
            router_drain_max_iterations=(
                int(os.getenv("ROUTER_DRAIN_MAX_ITERATIONS"))
                if os.getenv("ROUTER_DRAIN_MAX_ITERATIONS")
                else None
            ),
            router_drain_iteration_multiplier=int(
                os.getenv("ROUTER_DRAIN_ITERATION_MULTIPLIER", "3")
            ),
            router_drain_iteration_floor=int(
                os.getenv("ROUTER_DRAIN_ITERATION_FLOOR", "8")
            ),
            llm_api_base_url=os.getenv("ROUTER_LLM_API_BASE_URL"),
            llm_api_key=os.getenv("ROUTER_LLM_API_KEY"),
            llm_auth_http_client_enabled=_parse_bool_env(
                "ROUTER_LLM_AUTH_HTTP_CLIENT_ENABLED",
                False,
            ),
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
            router_agent_barrier_enabled=_parse_bool_env(ROUTER_AGENT_BARRIER_ENABLED_ENV, False),
        )
