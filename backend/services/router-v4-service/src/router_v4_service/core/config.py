from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

from router_v4_service.core.models import ContextPolicy

ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class RouterV4LLMSettings:
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = 30.0
    temperature: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    structured_output_method: str = "json_mode"

    @property
    def ready(self) -> bool:
        return bool(self.api_base_url and self.model)


@dataclass(frozen=True, slots=True)
class RouterV4Settings:
    """Runtime settings for the standalone v4 router service."""

    spec_root: Path | None = None
    state_dir: Path | None = None
    context_policy: ContextPolicy = ContextPolicy()
    recognizer_backend: str = "llm"
    fallback_agent_id: str = "fallback-agent"
    llm: RouterV4LLMSettings = field(default_factory=RouterV4LLMSettings)

    @classmethod
    def from_env(cls) -> "RouterV4Settings":
        """Load optional settings from process environment."""
        spec_root = _optional_path(os.environ.get("ROUTER_V4_SPEC_ROOT"))
        state_dir = _optional_path(os.environ.get("ROUTER_V4_STATE_DIR"))
        recognizer_backend = (
            _env_first("ROUTER_V4_RECOGNIZER_BACKEND", "ROUTER_RECOGNIZER_BACKEND") or "llm"
        ).strip().lower()
        return cls(
            spec_root=spec_root,
            state_dir=state_dir,
            fallback_agent_id=_env_first("ROUTER_V4_FALLBACK_AGENT_ID") or "fallback-agent",
            context_policy=ContextPolicy(
                max_chars=_positive_int("ROUTER_V4_CONTEXT_MAX_CHARS", 4000),
                recent_turn_limit=_positive_int("ROUTER_V4_RECENT_TURNS", 6),
                retrieved_reference_limit=_positive_int("ROUTER_V4_RETRIEVAL_LIMIT", 3),
            ),
            recognizer_backend=recognizer_backend,
            llm=RouterV4LLMSettings(
                api_base_url=_env_first("ROUTER_V4_LLM_API_BASE_URL", "ROUTER_LLM_API_BASE_URL"),
                api_key=_env_first("ROUTER_V4_LLM_API_KEY", "ROUTER_LLM_API_KEY"),
                model=_env_first("ROUTER_V4_LLM_MODEL", "ROUTER_LLM_RECOGNIZER_MODEL", "ROUTER_LLM_MODEL"),
                timeout_seconds=_positive_float("ROUTER_V4_LLM_TIMEOUT_SECONDS", _positive_float("ROUTER_LLM_TIMEOUT_SECONDS", 30.0)),
                temperature=_float_value("ROUTER_V4_LLM_TEMPERATURE", _float_value("ROUTER_LLM_TEMPERATURE", 0.0)),
                headers=_json_headers("ROUTER_V4_LLM_HEADERS_JSON") or _json_headers("ROUTER_LLM_HEADERS_JSON"),
                structured_output_method=_env_first(
                    "ROUTER_V4_LLM_STRUCTURED_OUTPUT_METHOD",
                    "ROUTER_LLM_STRUCTURED_OUTPUT_METHOD",
                ) or "json_mode",
            ),
        )


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _optional_path(raw: str | None) -> Path | None:
    if raw is None or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_value(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _json_headers(name: str) -> dict[str, str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def load_env_file(path: str | Path) -> None:
    """Load simple KEY=VALUE lines without executing shell syntax."""
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.match(key):
            continue
        os.environ.setdefault(key, _strip_env_value(value.strip()))


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
