from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, Field


class RouterDiagnosticCode(StrEnum):
    """Stable diagnostic codes returned to API callers and snapshots."""

    RECOGNIZER_LLM_FAILED_FALLBACK = "RECOGNIZER_LLM_FAILED_FALLBACK"
    GRAPH_BUILDER_LLM_FAILED_LEGACY_CHAIN = "GRAPH_BUILDER_LLM_FAILED_LEGACY_CHAIN"
    GRAPH_BUILDER_INVALID_PAYLOAD_LEGACY_CHAIN = "GRAPH_BUILDER_INVALID_PAYLOAD_LEGACY_CHAIN"
    GRAPH_BUILDER_EMPTY_GRAPH_FALLBACK_PLANNER = "GRAPH_BUILDER_EMPTY_GRAPH_FALLBACK_PLANNER"
    GRAPH_PLANNER_LLM_FAILED_FALLBACK = "GRAPH_PLANNER_LLM_FAILED_FALLBACK"
    GRAPH_PLANNER_EMPTY_GRAPH_FALLBACK = "GRAPH_PLANNER_EMPTY_GRAPH_FALLBACK"
    SLOT_EXTRACTOR_LLM_RETRYABLE_UNAVAILABLE = "SLOT_EXTRACTOR_LLM_RETRYABLE_UNAVAILABLE"
    SLOT_EXTRACTOR_LLM_FAILED_HEURISTIC_ONLY = "SLOT_EXTRACTOR_LLM_FAILED_HEURISTIC_ONLY"
    TURN_RECOGNITION_RETRYABLE_UNAVAILABLE = "TURN_RECOGNITION_RETRYABLE_UNAVAILABLE"
    ROUTER_NO_MATCH = "ROUTER_NO_MATCH"
    SLOT_REQUIRED_MISSING = "SLOT_REQUIRED_MISSING"
    SLOT_AMBIGUOUS = "SLOT_AMBIGUOUS"
    SLOT_INVALID = "SLOT_INVALID"


class RouterDiagnostic(BaseModel):
    """One machine-readable diagnostic attached to a response or snapshot."""

    code: str
    source: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


def diagnostic(
    code: RouterDiagnosticCode | str,
    *,
    source: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> RouterDiagnostic:
    """Build one normalized diagnostic record."""
    return RouterDiagnostic(
        code=str(code),
        source=source,
        message=message,
        details=details or {},
    )


def merge_diagnostics(*groups: Iterable[RouterDiagnostic] | None) -> list[RouterDiagnostic]:
    """Merge diagnostics while keeping a stable order and removing duplicates."""
    merged: list[RouterDiagnostic] = []
    seen: set[str] = set()
    for group in groups:
        if group is None:
            continue
        for item in group:
            marker = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(item)
    return merged
