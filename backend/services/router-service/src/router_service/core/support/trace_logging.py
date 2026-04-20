from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import time
from typing import Any, Iterator
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RouterTraceContext:
    """Per-request trace context shared by all nested router logs."""

    trace_id: str
    entrypoint: str
    session_id: str | None
    cust_id: str | None
    content_preview: str | None


_ROUTER_TRACE_CONTEXT: ContextVar[RouterTraceContext | None] = ContextVar(
    "router_trace_context",
    default=None,
)


def summarize_content(content: str | None, *, limit: int = 120) -> str | None:
    """Collapse one message into a single-line preview suitable for INFO logs."""
    if content is None:
        return None
    normalized = " ".join(part for part in content.split() if part).strip()
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def current_trace_context() -> RouterTraceContext | None:
    """Return the current router trace context, if any."""
    return _ROUTER_TRACE_CONTEXT.get()


def current_trace_id() -> str | None:
    """Return the current router trace id, if any."""
    context = current_trace_context()
    return context.trace_id if context is not None else None


@contextmanager
def router_trace(
    logger: logging.Logger,
    *,
    entrypoint: str,
    session_id: str | None,
    cust_id: str | None,
    content: str | None = None,
    details: dict[str, Any] | None = None,
) -> Iterator[RouterTraceContext]:
    """Create one top-level router trace and log its overall elapsed time."""
    if not logger.isEnabledFor(logging.INFO):
        context = RouterTraceContext(
            trace_id="",
            entrypoint=entrypoint,
            session_id=session_id,
            cust_id=cust_id,
            content_preview=None,
        )
        try:
            yield context
        except Exception:
            logger.exception(
                "Router trace failed (entrypoint=%s, session_id=%s, details=%s)",
                entrypoint,
                session_id,
                details or None,
            )
            raise
        return

    context = RouterTraceContext(
        trace_id=uuid4().hex[:8],
        entrypoint=entrypoint,
        session_id=session_id,
        cust_id=cust_id,
        content_preview=summarize_content(content),
    )
    token = _ROUTER_TRACE_CONTEXT.set(context)
    started_at = time.perf_counter()
    logger.info(
        "Router trace started (trace_id=%s, entrypoint=%s, session_id=%s, cust_id=%s, content_preview=%s, details=%s)",
        context.trace_id,
        context.entrypoint,
        context.session_id,
        context.cust_id,
        context.content_preview,
        details or None,
    )
    try:
        yield context
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "Router trace failed (trace_id=%s, entrypoint=%s, session_id=%s, elapsed_ms=%.2f, details=%s)",
            context.trace_id,
            context.entrypoint,
            context.session_id,
            elapsed_ms,
            details or None,
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "Router trace completed (trace_id=%s, entrypoint=%s, session_id=%s, elapsed_ms=%.2f, details=%s)",
            context.trace_id,
            context.entrypoint,
            context.session_id,
            elapsed_ms,
            details or None,
        )
    finally:
        _ROUTER_TRACE_CONTEXT.reset(token)


@contextmanager
def router_stage(
    logger: logging.Logger,
    stage: str,
    **details: Any,
) -> Iterator[None]:
    """Log one nested router stage with elapsed time under the current trace."""
    if not logger.isEnabledFor(logging.DEBUG):
        try:
            yield
        except Exception:
            logger.exception(
                "Router stage failed (stage=%s, details=%s)",
                stage,
                details or None,
            )
            raise
        return

    context = current_trace_context()
    trace_id = context.trace_id if context is not None else None
    session_id = context.session_id if context is not None else None
    started_at = time.perf_counter()
    logger.debug(
        "Router stage started (trace_id=%s, session_id=%s, stage=%s, details=%s)",
        trace_id,
        session_id,
        stage,
        details or None,
    )
    try:
        yield
    except Exception:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.exception(
            "Router stage failed (trace_id=%s, session_id=%s, stage=%s, elapsed_ms=%.2f, details=%s)",
            trace_id,
            session_id,
            stage,
            elapsed_ms,
            details or None,
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.debug(
            "Router stage completed (trace_id=%s, session_id=%s, stage=%s, elapsed_ms=%.2f, details=%s)",
            trace_id,
            session_id,
            stage,
            elapsed_ms,
            details or None,
        )
