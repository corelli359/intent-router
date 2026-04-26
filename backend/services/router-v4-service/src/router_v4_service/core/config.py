from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from router_v4_service.core.models import ContextPolicy


@dataclass(frozen=True, slots=True)
class RouterV4Settings:
    """Runtime settings for the standalone v4 router service."""

    spec_root: Path | None = None
    state_dir: Path | None = None
    context_policy: ContextPolicy = ContextPolicy()

    @classmethod
    def from_env(cls) -> "RouterV4Settings":
        """Load optional settings from process environment."""
        spec_root = _optional_path(os.environ.get("ROUTER_V4_SPEC_ROOT"))
        state_dir = _optional_path(os.environ.get("ROUTER_V4_STATE_DIR"))
        return cls(
            spec_root=spec_root,
            state_dir=state_dir,
            context_policy=ContextPolicy(
                max_chars=_positive_int("ROUTER_V4_CONTEXT_MAX_CHARS", 4000),
                recent_turn_limit=_positive_int("ROUTER_V4_RECENT_TURNS", 6),
                retrieved_reference_limit=_positive_int("ROUTER_V4_RETRIEVAL_LIMIT", 3),
            ),
        )


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
