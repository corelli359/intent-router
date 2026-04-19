from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable

from collections.abc import Mapping

from router_service.models.intent import IntentRecord, IntentStatus
from router_service.catalog.intent_repository import IntentRepository
from router_service.core.shared.domain import IntentDefinition, IntentDomain


_EMPTY_ACTIVE_INTENT_INDEX: Mapping[str, IntentDefinition] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """Immutable snapshot of active intents, fallback intent, and domain views."""

    active: tuple[IntentDefinition, ...] = ()
    active_by_code: Mapping[str, IntentDefinition] = field(
        default_factory=lambda: _EMPTY_ACTIVE_INTENT_INDEX
    )
    fallback: IntentDefinition | None = None
    priorities: dict[str, int] = field(default_factory=dict)
    domains: dict[str, IntentDomain] = field(default_factory=dict)


def build_intent_domains(intents: Iterable[IntentDefinition]) -> dict[str, IntentDomain]:
    """Group active leaf intents into domain-level routing views."""
    domain_groups: dict[str, list[IntentDefinition]] = defaultdict(list)
    for intent in intents:
        if not intent.is_leaf_intent or intent.is_fallback:
            continue
        domain_code = intent.domain_code.strip() or intent.intent_code
        domain_groups[domain_code].append(intent)

    views: dict[str, IntentDomain] = {}
    for code, leaves in sorted(domain_groups.items()):
        domain_name = next((leaf.domain_name.strip() for leaf in leaves if leaf.domain_name.strip()), code)
        domain_description = next(
            (leaf.domain_description.strip() for leaf in leaves if leaf.domain_description.strip()),
            "",
        )
        routing_examples: list[str] = []
        for leaf in sorted(leaves, key=lambda item: (-item.dispatch_priority, item.intent_code)):
            candidates = [*leaf.routing_examples, *leaf.examples]
            for example in candidates:
                normalized = str(example).strip()
                if normalized and normalized not in routing_examples:
                    routing_examples.append(normalized)
        leaf_intents = tuple(sorted(leaves, key=lambda item: (-item.dispatch_priority, item.intent_code)))
        dispatch_priority = max(leaf.dispatch_priority for leaf in leaves)
        views[code] = IntentDomain(
            domain_code=code,
            domain_name=domain_name,
            domain_description=domain_description,
            routing_examples=tuple(routing_examples),
            leaf_intents=leaf_intents,
            dispatch_priority=dispatch_priority,
        )
    return views


class RepositoryIntentCatalog:
    """Read-optimized catalog facade backed by the admin intent repository."""

    def __init__(self, repository: IntentRepository) -> None:
        """Initialize the catalog with an empty snapshot."""
        self.repository = repository
        self._snapshot = CatalogSnapshot()

    def list_active(self) -> list[IntentDefinition]:
        """Return all currently active routable leaf intents."""
        return list(self._snapshot.active)

    def list_active_domains(self) -> list[IntentDomain]:
        """Return all active domains derived from active leaf intents."""
        return list(self._snapshot.domains.values())

    def active_intents_by_code(self) -> Mapping[str, IntentDefinition]:
        """Return the cached intent lookup keyed by `intent_code`."""
        return self._snapshot.active_by_code

    def get_active_intent(self, intent_code: str) -> IntentDefinition | None:
        """Return the cached intent definition for one `intent_code`."""
        return self._snapshot.active_by_code.get(intent_code)

    def list_active_leaf_intents(self, domain_code: str) -> list[IntentDefinition]:
        """Return the active leaf intents under one domain."""
        domain = self._snapshot.domains.get(domain_code)
        if domain is None:
            return []
        return list(domain.leaf_intents)

    def priorities(self) -> dict[str, int]:
        """Return dispatch priorities by intent code."""
        return dict(self._snapshot.priorities)

    def get_fallback_intent(self) -> IntentDefinition | None:
        """Return a deep-copied fallback intent when one is configured."""
        if self._snapshot.fallback is None:
            return None
        return self._snapshot.fallback.model_copy(deep=True)

    def refresh_now(self) -> list[IntentDefinition]:
        """Refresh the catalog snapshot from the repository."""
        active_records = self.repository.list_intents(IntentStatus.ACTIVE)
        routable_intents: list[IntentDefinition] = []
        fallback_intents: list[IntentDefinition] = []
        for record in active_records:
            definition = self._to_definition(record)
            if definition.is_fallback:
                fallback_intents.append(definition)
            elif definition.is_leaf_intent:
                routable_intents.append(definition)

        fallback_intents.sort(key=lambda intent: intent.dispatch_priority, reverse=True)
        fallback_intent = fallback_intents[0] if fallback_intents else None
        priorities = {
            intent.intent_code: intent.dispatch_priority
            for intent in [*routable_intents, *fallback_intents]
        }
        active_by_code = MappingProxyType({intent.intent_code: intent for intent in routable_intents})
        self._snapshot = CatalogSnapshot(
            active=tuple(routable_intents),
            active_by_code=active_by_code,
            fallback=fallback_intent,
            priorities=priorities,
            domains=build_intent_domains(routable_intents),
        )
        return list(self._snapshot.active)

    def _to_definition(self, record: IntentRecord) -> IntentDefinition:
        """Convert an admin record into the runtime intent definition model."""
        return IntentDefinition(
            intent_code=record.intent_code,
            name=record.name,
            description=record.description,
            domain_code=record.domain_code,
            domain_name=record.domain_name,
            domain_description=record.domain_description,
            examples=record.examples,
            agent_url=record.agent_url,
            status=record.status.value,
            is_fallback=record.is_fallback,
            dispatch_priority=record.dispatch_priority,
            request_schema=record.request_schema,
            field_mapping=record.field_mapping,
            field_catalog=record.field_catalog,
            slot_schema=record.slot_schema,
            graph_build_hints=record.graph_build_hints,
            resume_policy=record.resume_policy,
            is_leaf_intent=record.is_leaf_intent,
            parent_intent_code=record.parent_intent_code,
            routing_examples=record.routing_examples,
        )
