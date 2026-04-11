from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
import json
from typing import Awaitable, Callable, Protocol

from router_core.domain import IntentDefinition, IntentMatch
from router_core.llm_client import IntentRecognitionPayload, JsonLLMClient, llm_exception_is_retryable
from router_core.prompt_templates import (
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
    build_recognizer_prompt,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecognitionResult:
    primary: list[IntentMatch]
    candidates: list[IntentMatch]


def recognition_intent_payload(intent: IntentDefinition) -> dict[str, object]:
    field_catalog = getattr(intent, "field_catalog", []) or []
    graph_build_hints = getattr(intent, "graph_build_hints", None)
    return {
        "intent_code": intent.intent_code,
        "name": intent.name,
        "description": intent.description,
        "examples": intent.examples,
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
    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult: ...


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
        return RecognitionResult(primary=[], candidates=[])


class LLMIntentRecognizer:
    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: IntentRecognizer | None = None,
        system_prompt_template: str = DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    ) -> None:
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
        active_intents = [intent for intent in intents if intent.status == "active"]
        if not active_intents:
            return RecognitionResult(primary=[], candidates=[])

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
            if llm_exception_is_retryable(exc):
                raise
            logger.warning(
                "LLM intent recognition failed, degrading to fallback recognizer (%s)",
                type(self.fallback).__name__,
                exc_info=True,
            )
            return await self.fallback.recognize(message, active_intents, recent_messages, long_term_memory)

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
            intent = definitions_by_code[match.intent_code]
            return (intent.dispatch_priority, match.confidence)

        primary.sort(key=_sort_key, reverse=True)
        candidates.sort(key=_sort_key, reverse=True)
        return RecognitionResult(primary=primary, candidates=candidates)
