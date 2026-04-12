from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Iterable

from router_service.core.shared.domain import IntentDefinition, IntentDomain, IntentGraphBuildHints
from router_service.core.recognition.recognizer import IntentMatch, IntentRecognizer, RecognitionResult


@dataclass(slots=True)
class DomainMatch:
    domain_code: str
    confidence: float
    reason: str


@dataclass(slots=True)
class DomainRoutingResult:
    primary: list[DomainMatch]
    candidates: list[DomainMatch]


class DomainRouter:
    def __init__(
        self,
        recognizer: IntentRecognizer,
        *,
        domain_primary_threshold: float = 0.65,
        domain_candidate_threshold: float = 0.4,
    ) -> None:
        self.recognizer = recognizer
        self.domain_primary_threshold = domain_primary_threshold
        self.domain_candidate_threshold = domain_candidate_threshold

    async def route(
        self,
        message: str,
        domains: Iterable[IntentDomain],
        *,
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> DomainRoutingResult:
        domain_list = list(domains)
        if not domain_list:
            return DomainRoutingResult(primary=[], candidates=[])
        recognition = await self.recognizer.recognize(
            message=message,
            intents=[self._to_intent(domain) for domain in domain_list],
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=on_delta,
        )
        return self._to_domain_result(recognition)

    def _to_intent(self, domain: IntentDomain) -> IntentDefinition:
        return IntentDefinition(
            intent_code=domain.domain_code,
            name=domain.domain_name or domain.domain_code,
            description=domain.domain_description or domain.domain_name or domain.domain_code,
            domain_code=domain.domain_code,
            domain_name=domain.domain_name,
            domain_description=domain.domain_description,
            examples=list(domain.routing_examples),
            keywords=[item for item in [domain.domain_name, domain.domain_code] if item],
            agent_url=f"domain://{domain.domain_code}",
            status="active",
            is_fallback=False,
            dispatch_priority=domain.dispatch_priority,
            primary_threshold=self.domain_primary_threshold,
            candidate_threshold=self.domain_candidate_threshold,
            request_schema={},
            field_mapping={},
            field_catalog=[],
            slot_schema=[],
            graph_build_hints=IntentGraphBuildHints(),
            resume_policy="resume_same_task",
            is_leaf_intent=False,
            parent_intent_code="",
            routing_examples=list(domain.routing_examples),
        )

    def _to_domain_result(self, recognition: RecognitionResult) -> DomainRoutingResult:
        def _to_domain_match(match: IntentMatch) -> DomainMatch:
            return DomainMatch(
                domain_code=match.intent_code,
                confidence=match.confidence,
                reason=match.reason,
            )

        return DomainRoutingResult(
            primary=[_to_domain_match(match) for match in recognition.primary],
            candidates=[_to_domain_match(match) for match in recognition.candidates],
        )
