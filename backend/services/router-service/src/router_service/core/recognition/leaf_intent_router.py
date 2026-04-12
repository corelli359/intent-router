from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Iterable

from router_service.core.domain import IntentDefinition
from router_service.core.recognizer import IntentMatch, IntentRecognizer, RecognitionResult


class LeafIntentRouter:
    def __init__(self, recognizer: IntentRecognizer) -> None:
        self.recognizer = recognizer

    async def route(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        allow_direct_single_leaf: bool = True,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        leaf_intents = list(intents)
        if not leaf_intents:
            return RecognitionResult(primary=[], candidates=[])
        if allow_direct_single_leaf and len(leaf_intents) == 1:
            intent = leaf_intents[0]
            match = IntentMatch(
                intent_code=intent.intent_code,
                confidence=0.99,
                reason="domain has a single leaf intent",
            )
            return RecognitionResult(primary=[match], candidates=[])
        return await self.recognizer.recognize(
            message=message,
            intents=leaf_intents,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=on_delta,
        )
