from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import re
from typing import Awaitable, Callable, Protocol

from router_service.core.shared.domain import IntentDefinition, IntentMatch
from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.support.llm_barrier import llm_barrier_triggered
from router_service.core.support.llm_client import IntentRecognitionPayload, JsonLLMClient, llm_exception_is_retryable
from router_service.core.prompts.prompt_templates import (
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
    build_recognizer_prompt,
)


logger = logging.getLogger(__name__)
_NORMALIZE_TEXT_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


@dataclass(slots=True)
class RecognitionResult:
    """Primary and candidate intent matches returned by recognition."""

    primary: list[IntentMatch]
    candidates: list[IntentMatch]
    diagnostics: list[RouterDiagnostic] | None = None


@dataclass(frozen=True, slots=True)
class _HeuristicIntentSpec:
    """Pre-normalized intent metadata reused across heuristic recognition calls."""

    intent_code: str
    dispatch_priority: int
    primary_threshold: float
    candidate_threshold: float
    candidate_texts: tuple[tuple[str, str], ...]
    has_currency_slot: bool
    has_required_slots: bool


def recognition_intent_payload(intent: IntentDefinition) -> dict[str, object]:
    """Convert one intent definition into the JSON payload consumed by the LLM prompt."""
    field_catalog = getattr(intent, "field_catalog", []) or []
    graph_build_hints = getattr(intent, "graph_build_hints", None)
    return {
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


class HeuristicIntentRecognizer:
    """Config-driven fallback recognizer used when model I/O is unavailable."""

    def __init__(
        self,
        *,
        minimum_similarity: float = 0.35,
        currency_slot_boost: float = 0.22,
        required_slot_boost: float = 0.05,
    ) -> None:
        self.minimum_similarity = minimum_similarity
        self.currency_slot_boost = currency_slot_boost
        self.required_slot_boost = required_slot_boost
        self._catalog_specs_cache: dict[tuple[int, ...], tuple[_HeuristicIntentSpec, ...]] = {}

    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        """Score intents from catalog text and slot metadata without calling an LLM."""
        del recent_messages, long_term_memory, on_delta
        message_text = _normalize_text(message)
        if not message_text:
            return RecognitionResult(primary=[], candidates=[], diagnostics=[])

        active_intents = [intent for intent in intents if intent.status == "active"]
        if not active_intents:
            return RecognitionResult(primary=[], candidates=[], diagnostics=[])
        specs = self._resolve_specs(active_intents)
        matches: list[IntentMatch] = []
        for spec in specs:
            confidence, best_text = self._score_spec(message_text=message_text, spec=spec)
            if confidence < self.minimum_similarity:
                continue
            matches.append(
                IntentMatch(
                    intent_code=spec.intent_code,
                    confidence=confidence,
                    reason=(
                        f"heuristic matched catalog text {best_text!r}"
                        if best_text
                        else "heuristic matched catalog metadata"
                    ),
                )
            )

        specs_by_code = {spec.intent_code: spec for spec in specs}

        def _sort_key(match: IntentMatch) -> tuple[int, float]:
            spec = specs_by_code.get(match.intent_code)
            return ((spec.dispatch_priority if spec is not None else 0), match.confidence)

        matches.sort(key=_sort_key, reverse=True)
        primary = [
            match
            for match in matches
            if self._confidence_threshold(match, specs_by_code, "primary")
        ]
        candidates = [
            match
            for match in matches
            if match not in primary and self._confidence_threshold(match, specs_by_code, "candidate")
        ]
        return RecognitionResult(primary=primary, candidates=candidates, diagnostics=[])

    def _resolve_specs(self, active_intents: list[IntentDefinition]) -> tuple[_HeuristicIntentSpec, ...]:
        """Build or reuse pre-normalized heuristic specs for one active catalog snapshot."""
        cache_key = tuple(id(intent) for intent in active_intents)
        cached = self._catalog_specs_cache.get(cache_key)
        if cached is not None:
            return cached
        specs = tuple(self._build_spec(intent) for intent in active_intents)
        if len(self._catalog_specs_cache) >= 8:
            self._catalog_specs_cache.clear()
        self._catalog_specs_cache[cache_key] = specs
        return specs

    def _build_spec(self, intent: IntentDefinition) -> _HeuristicIntentSpec:
        """Normalize the subset of intent metadata used by heuristic recognition."""
        return _HeuristicIntentSpec(
            intent_code=intent.intent_code,
            dispatch_priority=intent.dispatch_priority,
            primary_threshold=intent.primary_threshold,
            candidate_threshold=intent.candidate_threshold,
            candidate_texts=tuple(
                (candidate_text, _normalize_text(candidate_text))
                for candidate_text in self._candidate_texts(intent)
            ),
            has_currency_slot=any(
                slot.slot_key == "amount" or str(slot.value_type) == "currency"
                for slot in intent.slot_schema
            ),
            has_required_slots=any(slot.required for slot in intent.slot_schema),
        )

    def _score_spec(
        self,
        *,
        message_text: str,
        spec: _HeuristicIntentSpec,
    ) -> tuple[float, str | None]:
        best_similarity = 0.0
        best_text: str | None = None
        for original_text, normalized_text in spec.candidate_texts:
            similarity = self._text_similarity(
                message_text=message_text,
                candidate_text=normalized_text,
            )
            if similarity > best_similarity:
                best_similarity = similarity
                best_text = original_text

        if best_similarity <= 0:
            return 0.0, best_text

        score = best_similarity
        if any(character.isdigit() for character in message_text) and spec.has_currency_slot:
            score += self.currency_slot_boost
        if spec.has_required_slots:
            score += self.required_slot_boost
        return round(min(0.99, score), 2), best_text

    def _candidate_texts(self, intent: IntentDefinition) -> list[str]:
        candidates: list[str] = []
        for value in (
            intent.name,
            intent.domain_name,
            *intent.examples,
            *intent.routing_examples,
            *intent.keywords,
        ):
            cleaned = value.strip()
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
        return candidates

    def _confidence_threshold(
        self,
        match: IntentMatch,
        specs_by_code: dict[str, _HeuristicIntentSpec],
        kind: str,
    ) -> bool:
        spec = specs_by_code.get(match.intent_code)
        if spec is None:
            return False
        threshold = spec.primary_threshold if kind == "primary" else spec.candidate_threshold
        return match.confidence >= threshold

    def _text_similarity(self, *, message_text: str, candidate_text: str) -> float:
        if not candidate_text:
            return 0.0
        similarity = SequenceMatcher(None, message_text, candidate_text).ratio()
        if candidate_text in message_text or message_text in candidate_text:
            similarity = max(
                similarity,
                min(0.82, 0.55 + (min(len(candidate_text), len(message_text)) * 0.03)),
            )
        return similarity


class LLMIntentRecognizer:
    """LLM-backed recognizer that emits primary and candidate intent matches."""

    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: IntentRecognizer | None = None,
        system_prompt_template: str = DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    ) -> None:
        """Initialize the recognizer and compile the selected prompt template."""
        self.llm_client = llm_client
        self.model = model
        self.fallback = fallback or NullIntentRecognizer()
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
        active_intents = [intent for intent in intents if intent.status == "active"]
        if not active_intents:
            return RecognitionResult(primary=[], candidates=[], diagnostics=[])
        if getattr(self.llm_client, "barrier_enabled", False):
            fallback_result = await self.fallback.recognize(
                message,
                active_intents,
                recent_messages,
                long_term_memory,
            )
            return RecognitionResult(
                primary=list(fallback_result.primary),
                candidates=list(fallback_result.candidates),
                diagnostics=merge_diagnostics(
                    fallback_result.diagnostics or [],
                    [
                        diagnostic(
                            RouterDiagnosticCode.RECOGNIZER_LLM_FAILED_FALLBACK,
                            source="recognizer",
                            message="意图识别已切换到无模型启发式路径",
                            details={
                                "fallback": type(self.fallback).__name__,
                                "barrier_enabled": True,
                            },
                        )
                    ],
                ),
            )

        try:
            raw_response = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "message": message,
                    "recent_messages_json": json.dumps(recent_messages, ensure_ascii=False, indent=2),
                    "long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False, indent=2),
                    "intents_json": json.dumps(
                        [recognition_intent_payload(intent) for intent in active_intents],
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
                model=self.model,
                on_delta=on_delta,
            )
            response = IntentRecognitionPayload.model_validate(raw_response)
        except Exception as exc:
            if llm_exception_is_retryable(exc) or llm_barrier_triggered(exc):
                raise
            logger.warning(
                "LLM intent recognition failed, degrading to fallback recognizer (%s)",
                type(self.fallback).__name__,
                exc_info=True,
            )
            fallback_result = await self.fallback.recognize(message, active_intents, recent_messages, long_term_memory)
            return RecognitionResult(
                primary=list(fallback_result.primary),
                candidates=list(fallback_result.candidates),
                diagnostics=merge_diagnostics(
                    fallback_result.diagnostics or [],
                    [
                        diagnostic(
                            RouterDiagnosticCode.RECOGNIZER_LLM_FAILED_FALLBACK,
                            source="recognizer",
                            message="意图识别 LLM 失败，已降级到兜底识别器",
                            details={
                                "fallback": type(self.fallback).__name__,
                                "error_type": type(exc).__name__,
                            },
                        )
                    ],
                ),
            )

        raw_matches = response.matches

        definitions_by_code = {intent.intent_code: intent for intent in active_intents}
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


def _normalize_text(value: str) -> str:
    """Normalize free-form text into a stable matching form for heuristic scoring."""
    return _NORMALIZE_TEXT_RE.sub("", value).lower()
