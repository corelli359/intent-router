from __future__ import annotations

import asyncio

from router_service.core.domain import IntentDefinition, IntentGraphBuildHints
from router_service.core.leaf_intent_router import LeafIntentRouter
from router_service.core.recognizer import IntentMatch, IntentRecognizer, RecognitionResult


class _RecordingRecognizer(IntentRecognizer):
    def __init__(self, result: RecognitionResult) -> None:
        self.result = result
        self.calls = 0

    async def recognize(
        self,
        message: str,
        intents: list[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta=None,
    ) -> RecognitionResult:
        self.calls += 1
        return self.result


def _intent(intent_code: str) -> IntentDefinition:
    return IntentDefinition(
        intent_code=intent_code,
        name=intent_code,
        description="desc",
        agent_url=f"http://agent/{intent_code}",
        dispatch_priority=0,
        slot_schema=[],
        graph_build_hints=IntentGraphBuildHints(),
    )


def test_leaf_router_skips_llm_for_single_leaf() -> None:
    async def run() -> None:
        recognizer = _RecordingRecognizer(result=RecognitionResult(primary=[], candidates=[]))
        router = LeafIntentRouter(recognizer)
        result = await router.route(
            message="some text",
            intents=[_intent("only_one")],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary
        assert result.primary[0].confidence == 0.99
        assert recognizer.calls == 0

    asyncio.run(run())


def test_leaf_router_delegates_multi_leaf() -> None:
    async def run() -> None:
        match = IntentMatch(intent_code="intent_x", confidence=0.88, reason="found")
        recognizer = _RecordingRecognizer(result=RecognitionResult(primary=[match], candidates=[]))
        router = LeafIntentRouter(recognizer)
        result = await router.route(
            message="choose",
            intents=[_intent("a"), _intent("b")],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary == [match]
        assert recognizer.calls == 1

    asyncio.run(run())
