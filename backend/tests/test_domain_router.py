from __future__ import annotations

import asyncio
from dataclasses import dataclass

from router_service.core.shared.domain import IntentDefinition, IntentDomain, IntentGraphBuildHints
from router_service.core.recognition.domain_router import DomainRouter, DomainRoutingResult
from router_service.core.recognition.recognizer import IntentMatch, RecognitionResult


@dataclass
class _DummyRecognizer:
    result: RecognitionResult
    last_intents: list[IntentDefinition] | None = None

    async def recognize(
        self,
        message: str,
        intents: list[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta=None,
    ) -> RecognitionResult:
        self.last_intents = intents
        return self.result


def _leaf_intent(intent_code: str) -> IntentDefinition:
    return IntentDefinition(
        intent_code=intent_code,
        name=intent_code,
        description="desc",
        agent_url=f"http://agent/{intent_code}",
        dispatch_priority=0,
        slot_schema=[],
        graph_build_hints=IntentGraphBuildHints(),
    )


def _domain(domain_code: str) -> IntentDomain:
    return IntentDomain(
        domain_code=domain_code,
        domain_name=f"name_{domain_code}",
        domain_description="domain desc",
        routing_examples=(f"route {domain_code}",),
        leaf_intents=(_leaf_intent(f"{domain_code}_leaf"),),
        dispatch_priority=1,
    )


def test_domain_router_extracts_domain_matches() -> None:
    async def run() -> None:
        recognizer = _DummyRecognizer(
            result=RecognitionResult(
                primary=[IntentMatch(intent_code="payment", confidence=0.92, reason="matched")],
                candidates=[IntentMatch(intent_code="transfer", confidence=0.4, reason="maybe")],
            )
        )
        router = DomainRouter(recognizer)
        result = await router.route(
            message="帮助我缴费",
            domains=[_domain("payment"), _domain("transfer")],
            recent_messages=[],
            long_term_memory=[],
        )
        assert [match.domain_code for match in result.primary] == ["payment"]
        assert [match.domain_code for match in result.candidates] == ["transfer"]
        assert recognizer.last_intents is not None
        assert recognizer.last_intents[0].agent_url.startswith("domain://")

    asyncio.run(run())


def test_domain_router_handles_empty_domain_list() -> None:
    async def run() -> None:
        recognizer = _DummyRecognizer(result=RecognitionResult(primary=[], candidates=[]))
        router = DomainRouter(recognizer)
        result = await router.route(
            message="anything",
            domains=[],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary == []
        assert result.candidates == []

    asyncio.run(run())
