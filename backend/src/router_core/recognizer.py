from __future__ import annotations

import logging
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


logger = logging.getLogger(__name__)

WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]{2,}")
PURE_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
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


def _normalize_phrase(value: str) -> str:
    return value.strip().lower()


def _expand_cjk_patterns(token: str) -> set[str]:
    if not PURE_CJK_RE.fullmatch(token):
        return set()
    expanded: set[str] = set()
    max_size = min(6, len(token))
    for size in range(2, max_size + 1):
        for start in range(0, len(token) - size + 1):
            chunk = token[start : start + size]
            if chunk not in GENERIC_TERMS:
                expanded.add(chunk)
    return expanded


def _tokenize_source(source: str, *, expand_cjk: bool) -> set[str]:
    patterns: set[str] = set()
    normalized = _normalize_phrase(source)
    if len(normalized) >= 2 and normalized not in GENERIC_TERMS:
        patterns.add(normalized)
    for token in WORD_RE.findall(normalized):
        token = token.strip().lower()
        if len(token) < 2 or token in GENERIC_TERMS:
            continue
        patterns.add(token)
        if expand_cjk:
            patterns.update(_expand_cjk_patterns(token))
    return patterns


def _matched_phrases(values: Iterable[str], search_space: str) -> list[str]:
    hits: list[str] = []
    for value in values:
        normalized = _normalize_phrase(value)
        if len(normalized) < 2 or normalized in GENERIC_TERMS:
            continue
        if normalized in search_space:
            hits.append(normalized)
    return sorted(set(hits))


def extract_patterns(intent: IntentDefinition) -> set[str]:
    patterns: set[str] = set()
    patterns.update(_tokenize_source(intent.name, expand_cjk=True))
    for example in intent.examples:
        patterns.update(_tokenize_source(example, expand_cjk=True))
    for keyword in intent.keywords:
        patterns.update(_tokenize_source(keyword, expand_cjk=True))
    patterns.update(_tokenize_source(intent.description, expand_cjk=False))
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
    """Keyword/phrase-based intent recognizer — **last-resort fallback only**.

    This recognizer uses regex tokenization and keyword matching.
    It cannot handle synonyms, paraphrasing, context-dependent expressions,
    or any form of semantic understanding.

    It should **never** be the primary production recognizer.  Use
    ``LLMIntentRecognizer`` for all production traffic; this class exists
    solely as a degraded fallback when the LLM service is entirely
    unavailable, or for offline/test scenarios.
    """

    def __init__(self, intent_catalog: object | None = None) -> None:
        self.intent_catalog = intent_catalog

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
        precomputed_patterns = self._patterns_by_intent_code()

        for intent in intents:
            if intent.status != "active":
                continue
            patterns = precomputed_patterns.get(intent.intent_code) or extract_patterns(intent)
            matches = sorted(pattern for pattern in patterns if pattern in search_space)
            if not matches:
                continue
            name_hits = _matched_phrases([intent.name], search_space)
            example_hits = _matched_phrases(intent.examples, search_space)
            keyword_hits = _matched_phrases(intent.keywords, search_space)
            strong_hits = set(name_hits) | set(example_hits) | set(keyword_hits)
            supporting_hits = [pattern for pattern in matches if pattern not in strong_hits]

            score = 0.38
            if name_hits:
                score += 0.32
            if example_hits:
                score += min(0.4, 0.24 * len(example_hits))
            if keyword_hits:
                score += min(0.32, 0.16 * len(keyword_hits))
            if supporting_hits:
                score += min(0.22, 0.08 * len(supporting_hits))

            if not strong_hits and len(matches) >= 2:
                score += 0.08
            score = min(0.99, round(score, 2))
            reason = f"matched phrases: {', '.join(matches[:5])}"
            scored.append((intent, score, reason))

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

    def _patterns_by_intent_code(self) -> dict[str, set[str]]:
        if self.intent_catalog is None:
            return {}
        getter = getattr(self.intent_catalog, "patterns", None)
        if getter is None:
            return {}
        patterns = getter()
        if not isinstance(patterns, dict):
            return {}
        normalized: dict[str, set[str]] = {}
        for intent_code, values in patterns.items():
            if not isinstance(intent_code, str):
                continue
            if not isinstance(values, set):
                values = set(values)
            normalized[intent_code] = {str(value).lower() for value in values}
        return normalized


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
