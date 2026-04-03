from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re

from router_core.domain import IntentDefinition, IntentMatch


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


class SimpleIntentRecognizer:
    """Direct phrase matcher for the MVP to keep routing deterministic."""

    def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
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
