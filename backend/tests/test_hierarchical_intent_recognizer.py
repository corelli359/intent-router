from __future__ import annotations

import asyncio

from router_service.core.shared.domain import IntentDefinition, IntentDomain, IntentGraphBuildHints
from router_service.core.recognition.domain_router import DomainMatch, DomainRoutingResult
from router_service.core.recognition.hierarchical_intent_recognizer import HierarchicalIntentRecognizer
from router_service.core.recognition.recognizer import IntentMatch, IntentRecognizer, RecognitionResult


def _intent(intent_code: str, *, domain_code: str = "", domain_name: str = "") -> IntentDefinition:
    return IntentDefinition(
        intent_code=intent_code,
        name=intent_code,
        description="desc",
        domain_code=domain_code,
        domain_name=domain_name,
        agent_url=f"http://agent/{intent_code}",
        dispatch_priority=0,
        slot_schema=[],
        graph_build_hints=IntentGraphBuildHints(),
    )


def _domain(domain_code: str) -> IntentDomain:
    return IntentDomain(
        domain_code=domain_code,
        domain_name=domain_code,
        domain_description="desc",
        routing_examples=(),
        leaf_intents=(_intent(f"{domain_code}_leaf"),),
        dispatch_priority=1,
    )


class _DomainCatalog:
    def __init__(self, domains: list[IntentDomain]) -> None:
        self._domains = domains

    def list_active_domains(self) -> list[IntentDomain]:
        return list(self._domains)


class _FakeDomainRouter:
    def __init__(self, result: DomainRoutingResult) -> None:
        self.result = result
        self.calls = 0

    async def route(self, *args, **kwargs) -> DomainRoutingResult:
        self.calls += 1
        return self.result


class _FakeLeafRouter:
    def __init__(self, result: RecognitionResult) -> None:
        self.result = result
        self.calls = 0

    async def route(self, *args, **kwargs) -> RecognitionResult:
        self.calls += 1
        return self.result


class _FakeFallbackRecognizer(IntentRecognizer):
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


def test_hierarchical_recognizer_uses_leaf_result() -> None:
    async def run() -> None:
        match = IntentMatch(intent_code="payment_leaf", confidence=0.9, reason="matched")
        domain_match = DomainMatch(domain_code="payment", confidence=0.8, reason="domain match")
        catalog = _DomainCatalog([_domain("payment"), _domain("transfer")])
        domain_router = _FakeDomainRouter(
            DomainRoutingResult(primary=[domain_match], candidates=[])
        )
        leaf_router = _FakeLeafRouter(RecognitionResult(primary=[match], candidates=[]))
        fallback = _FakeFallbackRecognizer(RecognitionResult(primary=[], candidates=[]))
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=catalog,
            domain_router=domain_router,
            leaf_router=leaf_router,
            fallback=fallback,
        )
        result = await recognizer.recognize(
            message="pay",
            intents=[
                _intent("payment_leaf", domain_code="payment", domain_name="payment"),
                _intent("transfer_leaf", domain_code="transfer", domain_name="transfer"),
            ],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary[0].intent_code == "payment_leaf"
        assert result.primary[0].confidence == 0.8
        assert fallback.calls == 0
        assert leaf_router.calls == 1
        assert domain_router.calls == 1

    asyncio.run(run())


def test_hierarchical_recognizer_falls_back_when_domain_missing() -> None:
    async def run() -> None:
        catalog = _DomainCatalog([])
        domain_router = _FakeDomainRouter(DomainRoutingResult(primary=[], candidates=[]))
        leaf_router = _FakeLeafRouter(RecognitionResult(primary=[], candidates=[]))
        fallback = _FakeFallbackRecognizer(
            RecognitionResult(primary=[IntentMatch(intent_code="fallback", confidence=0.7, reason="flat")], candidates=[])
        )
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=catalog,
            domain_router=domain_router,
            leaf_router=leaf_router,
            fallback=fallback,
        )
        result = await recognizer.recognize(
            message="hello",
            intents=[],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary[0].intent_code == "fallback"
        assert fallback.calls == 1

    asyncio.run(run())


def test_hierarchical_recognizer_falls_back_when_leaf_router_empty() -> None:
    async def run() -> None:
        catalog = _DomainCatalog([_domain("payment"), _domain("transfer")])
        domain_router = _FakeDomainRouter(
            DomainRoutingResult(primary=[DomainMatch(domain_code="payment", confidence=0.6, reason="maybe")], candidates=[])
        )
        leaf_router = _FakeLeafRouter(RecognitionResult(primary=[], candidates=[]))
        fallback = _FakeFallbackRecognizer(
            RecognitionResult(primary=[IntentMatch(intent_code="fallback", confidence=0.5, reason="flat")], candidates=[])
        )
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=catalog,
            domain_router=domain_router,
            leaf_router=leaf_router,
            fallback=fallback,
        )
        result = await recognizer.recognize(
            message="hello",
            intents=[],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary[0].intent_code == "fallback"
        assert fallback.calls == 1

    asyncio.run(run())


def test_hierarchical_recognizer_routes_single_domain_directly_to_leaf_router() -> None:
    async def run() -> None:
        catalog = _DomainCatalog([_domain("payment")])
        domain_router = _FakeDomainRouter(DomainRoutingResult(primary=[], candidates=[]))
        leaf_router = _FakeLeafRouter(
            RecognitionResult(primary=[IntentMatch(intent_code="payment_leaf", confidence=0.91, reason="leaf")], candidates=[])
        )
        fallback = _FakeFallbackRecognizer(RecognitionResult(primary=[], candidates=[]))
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=catalog,
            domain_router=domain_router,
            leaf_router=leaf_router,
            fallback=fallback,
        )
        result = await recognizer.recognize(
            message="pay",
            intents=[
                _intent("payment_leaf", domain_code="payment", domain_name="payment"),
                _intent("payment_alt", domain_code="payment", domain_name="payment"),
            ],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary[0].intent_code == "payment_leaf"
        assert domain_router.calls == 0
        assert leaf_router.calls == 1
        assert fallback.calls == 0

    asyncio.run(run())

def test_hierarchical_recognizer_keeps_candidate_when_domain_is_weak() -> None:
    async def run() -> None:
        catalog = _DomainCatalog([_domain("payment"), _domain("transfer")])
        domain_router = _FakeDomainRouter(
            DomainRoutingResult(candidates=[DomainMatch(domain_code="payment", confidence=0.55, reason="maybe")], primary=[])
        )
        leaf_router = _FakeLeafRouter(
            RecognitionResult(primary=[IntentMatch(intent_code="payment_leaf", confidence=0.93, reason="leaf")], candidates=[])
        )
        fallback = _FakeFallbackRecognizer(RecognitionResult(primary=[], candidates=[]))
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=catalog,
            domain_router=domain_router,
            leaf_router=leaf_router,
            fallback=fallback,
        )
        result = await recognizer.recognize(
            message="pay",
            intents=[
                _intent("payment_leaf", domain_code="payment", domain_name="payment"),
                _intent("transfer_leaf", domain_code="transfer", domain_name="transfer"),
            ],
            recent_messages=[],
            long_term_memory=[],
        )
        assert result.primary == []
        assert result.candidates[0].intent_code == "payment_leaf"
        assert result.candidates[0].confidence == 0.55

    asyncio.run(run())
