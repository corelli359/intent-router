from __future__ import annotations


ROUTER_LLM_BARRIER_ENABLED_ENV = "ROUTER_LLM_BARRIER_ENABLED"


class LLMBarrierTriggeredError(RuntimeError):
    """Raised when perf mode blocks a call that would perform real LLM I/O."""


def build_llm_barrier_error(
    *,
    model: str | None,
    prompt_name: str,
    base_url: str | None,
) -> LLMBarrierTriggeredError:
    """Build a stable, operator-friendly error for blocked LLM calls."""
    return LLMBarrierTriggeredError(
        f"{ROUTER_LLM_BARRIER_ENABLED_ENV}=true blocked a real LLM call "
        f"(model={model or 'unset'}, prompt={prompt_name}, base_url={base_url or 'unset'}). "
        "Perf traffic must use router-only paths and must not trigger model I/O."
    )


def llm_barrier_triggered(exc: BaseException) -> bool:
    """Return whether the raised exception came from the perf LLM barrier."""
    return isinstance(exc, LLMBarrierTriggeredError)
