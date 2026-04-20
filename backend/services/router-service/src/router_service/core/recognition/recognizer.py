from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from router_service.core.shared.domain import IntentDefinition, IntentMatch
from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
)
from router_service.core.support.json_codec import json_dumps
from router_service.core.support.llm_client import IntentRecognitionPayload, JsonLLMClient, llm_exception_is_retryable
from router_service.core.prompts.prompt_templates import (
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
    build_recognizer_prompt,
)


logger = logging.getLogger(__name__)
_INTENT_PAYLOAD_CACHE_LIMIT = 2048
_INTENTS_JSON_CACHE_LIMIT = 256
_INTENTS_BY_CODE_CACHE_LIMIT = 256
_intent_payload_cache: dict[int, tuple[IntentDefinition, dict[str, object]]] = {}
_intents_json_cache: dict[tuple[int, ...], tuple[tuple[IntentDefinition, ...], str]] = {}
_intents_by_code_cache: dict[tuple[int, ...], tuple[tuple[IntentDefinition, ...], dict[str, IntentDefinition]]] = {}


@dataclass(slots=True)
class RecognitionResult:
    """Primary and candidate intent matches returned by recognition."""

    primary: list[IntentMatch]
    candidates: list[IntentMatch]
    diagnostics: list[RouterDiagnostic] | None = None


def recognition_intent_payload(intent: IntentDefinition) -> dict[str, object]:
    """Convert one intent definition into the JSON payload consumed by the LLM prompt."""
    cached = _intent_payload_cache.get(id(intent))
    if cached is not None and cached[0] is intent:
        return cached[1]
    field_catalog = getattr(intent, "field_catalog", []) or []
    graph_build_hints = getattr(intent, "graph_build_hints", None)
    payload = {
        "intent_code": intent.intent_code,
        "name": intent.name,
        "description": intent.description,
        "domain_code": getattr(intent, "domain_code", ""),
        "domain_name": getattr(intent, "domain_name", ""),
        "domain_description": getattr(intent, "domain_description", ""),
        "examples": intent.examples,
        "routing_examples": getattr(intent, "routing_examples", []),
        "keywords": intent.keywords,
        "dispatch_priority": intent.dispatch_priority,
        "primary_threshold": intent.primary_threshold,
        "candidate_threshold": intent.candidate_threshold,
        "field_catalog": [
            field.model_dump(mode="json") if hasattr(field, "model_dump") else dict(field)
            for field in field_catalog
        ],
        "slot_schema": [slot.model_dump(mode="json") for slot in intent.slot_schema],
        "graph_build_hints": (
            graph_build_hints.model_dump(mode="json")
            if hasattr(graph_build_hints, "model_dump")
            else dict(graph_build_hints or {})
        ),
    }
    if len(_intent_payload_cache) >= _INTENT_PAYLOAD_CACHE_LIMIT:
        _intent_payload_cache.clear()
    _intent_payload_cache[id(intent)] = (intent, payload)
    return payload


def recognition_intents_json(intents: Iterable[IntentDefinition]) -> str:
    """Serialize one active-intent set to JSON, reusing cached payloads across requests."""
    intent_tuple = tuple(intents)
    cache_key = tuple(id(intent) for intent in intent_tuple)
    cached = _intents_json_cache.get(cache_key)
    if cached is not None:
        cached_intents, cached_json = cached
        if len(cached_intents) == len(intent_tuple) and all(left is right for left, right in zip(cached_intents, intent_tuple)):
            return cached_json
    payload = json_dumps([recognition_intent_payload(intent) for intent in intent_tuple])
    if len(_intents_json_cache) >= _INTENTS_JSON_CACHE_LIMIT:
        _intents_json_cache.clear()
    _intents_json_cache[cache_key] = (intent_tuple, payload)
    return payload


def recognition_intents_by_code(intents: Iterable[IntentDefinition]) -> dict[str, IntentDefinition]:
    """Return a cached intent lookup for one active-intent set."""
    intent_tuple = tuple(intents)
    cache_key = tuple(id(intent) for intent in intent_tuple)
    cached = _intents_by_code_cache.get(cache_key)
    if cached is not None:
        cached_intents, cached_index = cached
        if len(cached_intents) == len(intent_tuple) and all(
            left is right for left, right in zip(cached_intents, intent_tuple)
        ):
            return cached_index
    index = {intent.intent_code: intent for intent in intent_tuple}
    if len(_intents_by_code_cache) >= _INTENTS_BY_CODE_CACHE_LIMIT:
        _intents_by_code_cache.clear()
    _intents_by_code_cache[cache_key] = (intent_tuple, index)
    return index


class IntentRecognizer(Protocol):
    """Protocol for components that can recognize intents from free-form messages."""

    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        """Recognize primary and candidate intents from one message."""
        ...


class NullIntentRecognizer:
    """Fail-closed recognizer used when semantic recognition must not degrade to rules."""

    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        """Return an empty recognition result without attempting semantic routing."""
        return RecognitionResult(primary=[], candidates=[], diagnostics=[])


class LLMIntentRecognizer:
    """LLM-backed recognizer that emits primary and candidate intent matches."""

    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        system_prompt_template: str = DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    ) -> None:
        """Initialize the recognizer and compile the selected prompt template."""
        self.llm_client = llm_client
        self.model = model
        self.prompt = build_recognizer_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        """Run LLM-based recognition and bucket matches by configured thresholds."""
        active_intents = tuple(intent for intent in intents if intent.status == "active")
        if not active_intents:
            return RecognitionResult(primary=[], candidates=[], diagnostics=[])

        try:
            raw_response = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "message": message,
                    "recent_messages_json": json_dumps(recent_messages),
                    "long_term_memory_json": json_dumps(long_term_memory),
                    "intents_json": recognition_intents_json(active_intents),
                },
                model=self.model,
                on_delta=on_delta,
            )
            response = IntentRecognitionPayload.model_validate(raw_response)
        except Exception as exc:
            if llm_exception_is_retryable(exc):
                raise
            logger.debug("LLM intent recognition failed, keeping an empty fail-closed result", exc_info=True)
            return RecognitionResult(
                primary=[],
                candidates=[],
                diagnostics=[
                    diagnostic(
                        RouterDiagnosticCode.RECOGNIZER_LLM_FAILED,
                        source="recognizer",
                        message="意图识别 LLM 失败，当前不执行本地兜底识别",
                        details={"error_type": type(exc).__name__},
                    )
                ],
            )

        raw_matches = response.matches

        definitions_by_code = recognition_intents_by_code(active_intents)
        primary: list[IntentMatch] = []
        candidates: list[IntentMatch] = []
        seen_codes: set[str] = set()

        for item in raw_matches:
            intent_code = str(item.intent_code).strip()
            intent = definitions_by_code.get(intent_code)
            if intent is None or intent_code in seen_codes:
                continue

            try:
                confidence = round(min(0.99, max(0.0, float(item.confidence))), 2)
            except (TypeError, ValueError):
                continue

            reason = str(item.reason or "llm matched the request")
            match = IntentMatch(intent_code=intent_code, confidence=confidence, reason=reason)
            if confidence >= intent.primary_threshold:
                primary.append(match)
            elif confidence >= intent.candidate_threshold:
                candidates.append(match)
            seen_codes.add(intent_code)

        def _sort_key(match: IntentMatch) -> tuple[int, float]:
            """Sort higher-priority and higher-confidence matches first."""
            intent = definitions_by_code[match.intent_code]
            return (intent.dispatch_priority, match.confidence)

        primary.sort(key=_sort_key, reverse=True)
        candidates.sort(key=_sort_key, reverse=True)
        return RecognitionResult(primary=primary, candidates=candidates, diagnostics=[])
