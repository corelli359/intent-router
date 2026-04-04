from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import re
from typing import Awaitable, Callable, Protocol

from router_core.domain import IntentDefinition, IntentMatch
from router_core.llm_client import IntentRecognitionPayload, JsonLLMClient
from router_core.prompt_templates import (
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
    build_recognizer_prompt,
)


WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")
GENERIC_TERMS = {
    "帮我",
    "一下",
    "请问",
    "请",
    "需要",
    "处理",
    "用户",
    "请求",
    "帮",
    "我",
    "一下子",
}


def extract_patterns(intent: IntentDefinition) -> set[str]:
    patterns: set[str] = set()
    sources = [intent.name, intent.description, *intent.examples, *intent.keywords]
    for source in sources:
        normalized = source.strip().lower()
        if len(normalized) >= 2 and normalized not in GENERIC_TERMS:
            patterns.add(normalized)
        for token in WORD_RE.findall(normalized):
            token = token.strip().lower()
            if len(token) >= 2 and token not in GENERIC_TERMS:
                patterns.add(token)
    return patterns


@dataclass(slots=True)
class RecognitionResult:
    primary: list[IntentMatch]
    candidates: list[IntentMatch]


def recognition_intent_payload(intent: IntentDefinition) -> dict[str, object]:
    return {
        "intent_code": intent.intent_code,
        "name": intent.name,
        "description": intent.description,
        "examples": intent.examples,
        "keywords": intent.keywords,
        "dispatch_priority": intent.dispatch_priority,
        "primary_threshold": intent.primary_threshold,
        "candidate_threshold": intent.candidate_threshold,
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


class SimpleIntentRecognizer:
    """Direct phrase matcher for the MVP to keep routing deterministic."""

    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        search_space = " ".join([message, *recent_messages[-2:], *long_term_memory]).lower()
        scored: list[tuple[IntentDefinition, float, str]] = []

        for intent in intents:
            if intent.status != "active":
                continue
            patterns = extract_patterns(intent)
            matches = sorted(pattern for pattern in patterns if pattern in search_space)
            if not matches:
                continue
            if intent.keywords:
                keyword_hits = [
                    keyword for keyword in intent.keywords if len(keyword.strip()) >= 2 and keyword.lower() in search_space
                ]
                if not keyword_hits:
                    continue
                score = 0.55 + min(0.4, 0.2 * len(keyword_hits))
            else:
                score = 0.3 + (len(matches) / max(1, min(3, len(patterns)))) * 0.6
            reason = f"matched phrases: {', '.join(matches[:5])}"
            scored.append((intent, min(0.99, round(score, 2)), reason))

        scored.sort(key=lambda item: (item[0].dispatch_priority, item[1]), reverse=True)
        primary: list[IntentMatch] = []
        candidates: list[IntentMatch] = []

        for intent, score, reason in scored:
            match = IntentMatch(intent_code=intent.intent_code, confidence=score, reason=reason)
            if score >= intent.primary_threshold:
                primary.append(match)
            elif score >= intent.candidate_threshold:
                candidates.append(match)

        return RecognitionResult(primary=primary, candidates=candidates)


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
        self.fallback = fallback or SimpleIntentRecognizer()
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
        except Exception:
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
