from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Awaitable, Callable

from router_core.domain import IntentDefinition, IntentMatch
from router_core.recognizer import RecognitionResult


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


class SimpleIntentRecognizer:
    """Legacy keyword recognizer for offline experiments only."""

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
