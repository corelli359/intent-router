from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from router_service.core.shared.domain import IntentDefinition, IntentDomain, IntentMatch
from router_service.core.recognition.domain_router import DomainMatch, DomainRouter, DomainRoutingResult
from router_service.core.support.intent_catalog import build_intent_domains
from router_service.core.recognition.leaf_intent_router import LeafIntentRouter
from router_service.core.recognition.recognizer import IntentRecognizer, RecognitionResult
from router_service.core.shared.diagnostics import merge_diagnostics


class HierarchicalIntentRecognizer:
    """Two-stage recognizer that routes by domain first, then by leaf intent."""

    def __init__(
        self,
        *,
        domain_router: DomainRouter,
        leaf_router: LeafIntentRouter,
        fallback: IntentRecognizer,
        intent_catalog: Any | None = None,
    ) -> None:
        """Initialize the hierarchical recognizer with domain, leaf, and fallback routers."""
        self.domain_router = domain_router
        self.leaf_router = leaf_router
        self.fallback = fallback
        self.intent_catalog = intent_catalog

    async def recognize(
        self,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> RecognitionResult:
        """Recognize intents by first narrowing down candidate domains."""
        active_intents = [
            intent
            for intent in intents
            if intent.status == "active" and intent.is_leaf_intent and not intent.is_fallback
        ]
        domains = self._resolve_domains(active_intents)
        if not domains:
            return await self._fallback(
                message=message,
                intents=intents,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                on_delta=on_delta,
            )

        if len(domains) == 1:
            domain = domains[0]
            return await self.leaf_router.route(
                message=message,
                intents=domain.leaf_intents,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                allow_direct_single_leaf=True,
                on_delta=on_delta,
            )

        domain_result = await self.domain_router.route(
            message=message,
            domains=domains,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=on_delta,
        )

        aggregated = await self._aggregate_leaf_results(
            domain_result=domain_result,
            domains=domains,
            message=message,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
        )
        if aggregated.primary or aggregated.candidates:
            return aggregated
        return await self._fallback(
            message=message,
            intents=intents,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=on_delta,
        )

    async def _aggregate_leaf_results(
        self,
        *,
        domain_result: DomainRoutingResult,
        domains: list[IntentDomain],
        message: str,
        recent_messages: list[str],
        long_term_memory: list[str],
    ) -> RecognitionResult:
        """Aggregate leaf recognition results across matched domains."""
        domain_index = {domain.domain_code: domain for domain in domains}
        dispatch_priorities = {
            intent.intent_code: intent.dispatch_priority
            for domain in domains
            for intent in domain.leaf_intents
        }
        best_by_code: dict[str, tuple[bool, float, str]] = {}
        diagnostics = []

        async def _merge_domain_matches(matches: list[DomainMatch], *, domain_is_primary: bool) -> None:
            """Merge leaf matches produced under one bucket of domain matches."""
            for domain_match in matches:
                domain = domain_index.get(domain_match.domain_code)
                if domain is None:
                    continue
                leaf_result = await self.leaf_router.route(
                    message=message,
                    intents=domain.leaf_intents,
                    recent_messages=recent_messages,
                    long_term_memory=long_term_memory,
                    allow_direct_single_leaf=True,
                )
                diagnostics.extend(leaf_result.diagnostics or [])
                self._merge_leaf_bucket(
                    best_by_code=best_by_code,
                    leaf_matches=leaf_result.primary,
                    domain_match=domain_match,
                    produce_primary=domain_is_primary,
                )
                self._merge_leaf_bucket(
                    best_by_code=best_by_code,
                    leaf_matches=leaf_result.candidates,
                    domain_match=domain_match,
                    produce_primary=False,
                )

        await _merge_domain_matches(domain_result.primary, domain_is_primary=True)
        await _merge_domain_matches(domain_result.candidates, domain_is_primary=False)

        primary: list[IntentMatch] = []
        candidates: list[IntentMatch] = []
        for intent_code, (is_primary, confidence, reason) in best_by_code.items():
            match = IntentMatch(intent_code=intent_code, confidence=confidence, reason=reason)
            if is_primary:
                primary.append(match)
            else:
                candidates.append(match)

        def _sort_key(match: IntentMatch) -> tuple[int, float, str]:
            """Sort higher-priority and higher-confidence matches first."""
            return (
                dispatch_priorities.get(match.intent_code, 0),
                match.confidence,
                match.intent_code,
            )

        primary.sort(key=_sort_key, reverse=True)
        candidates.sort(key=_sort_key, reverse=True)
        return RecognitionResult(
            primary=primary,
            candidates=candidates,
            diagnostics=merge_diagnostics(diagnostics),
        )

    def _merge_leaf_bucket(
        self,
        *,
        best_by_code: dict[str, tuple[bool, float, str]],
        leaf_matches: list[IntentMatch],
        domain_match: DomainMatch,
        produce_primary: bool,
    ) -> None:
        """Merge one batch of leaf matches into the best-by-code accumulator."""
        for leaf_match in leaf_matches:
            confidence = round(min(domain_match.confidence, leaf_match.confidence), 2)
            reason = (
                f"domain={domain_match.domain_code}({domain_match.reason}); "
                f"leaf={leaf_match.reason}"
            )
            existing = best_by_code.get(leaf_match.intent_code)
            new_value = (produce_primary, confidence, reason)
            if existing is None or self._is_better(new_value, existing):
                best_by_code[leaf_match.intent_code] = new_value

    def _is_better(
        self,
        candidate: tuple[bool, float, str],
        current: tuple[bool, float, str],
    ) -> bool:
        """Return whether one aggregated match should replace the current best match."""
        candidate_primary, candidate_confidence, _ = candidate
        current_primary, current_confidence, _ = current
        if candidate_primary != current_primary:
            return candidate_primary and not current_primary
        return candidate_confidence > current_confidence

    def _resolve_domains(self, active_intents: list[IntentDefinition]) -> list[IntentDomain]:
        """Resolve domain views from active intents or the shared catalog."""
        domains = list(build_intent_domains(active_intents).values())
        if domains:
            return domains
        if self.intent_catalog is None:
            return []
        return list(self.intent_catalog.list_active_domains())

    async def _fallback(
        self,
        *,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        on_delta: Callable[[str], Awaitable[None]] | None,
    ) -> RecognitionResult:
        """Delegate recognition to the fallback recognizer when hierarchy is unavailable."""
        return await self.fallback.recognize(
            message=message,
            intents=intents,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=on_delta,
        )
