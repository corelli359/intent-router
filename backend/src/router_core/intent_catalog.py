from __future__ import annotations

from dataclasses import dataclass, field

from models.intent import IntentRecord, IntentStatus
from persistence.intent_repository import IntentRepository
from router_core.domain import IntentDefinition


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    active: tuple[IntentDefinition, ...] = ()
    fallback: IntentDefinition | None = None
    priorities: dict[str, int] = field(default_factory=dict)


class RepositoryIntentCatalog:
    def __init__(self, repository: IntentRepository) -> None:
        self.repository = repository
        self._snapshot = CatalogSnapshot()

    def list_active(self) -> list[IntentDefinition]:
        return list(self._snapshot.active)

    def priorities(self) -> dict[str, int]:
        return dict(self._snapshot.priorities)

    def get_fallback_intent(self) -> IntentDefinition | None:
        if self._snapshot.fallback is None:
            return None
        return self._snapshot.fallback.model_copy(deep=True)

    def refresh_now(self) -> list[IntentDefinition]:
        active_records = self.repository.list_intents(IntentStatus.ACTIVE)
        routable_intents: list[IntentDefinition] = []
        fallback_intents: list[IntentDefinition] = []
        for record in active_records:
            definition = self._to_definition(record)
            if definition.is_fallback:
                fallback_intents.append(definition)
            else:
                routable_intents.append(definition)

        fallback_intents.sort(key=lambda intent: intent.dispatch_priority, reverse=True)
        fallback_intent = fallback_intents[0] if fallback_intents else None
        priorities = {
            intent.intent_code: intent.dispatch_priority
            for intent in [*routable_intents, *fallback_intents]
        }
        self._snapshot = CatalogSnapshot(
            active=tuple(routable_intents),
            fallback=fallback_intent,
            priorities=priorities,
        )
        return list(self._snapshot.active)

    def _to_definition(self, record: IntentRecord) -> IntentDefinition:
        return IntentDefinition(
            intent_code=record.intent_code,
            name=record.name,
            description=record.description,
            examples=record.examples,
            agent_url=record.agent_url,
            status=record.status.value,
            is_fallback=record.is_fallback,
            dispatch_priority=record.dispatch_priority,
            request_schema=record.request_schema,
            field_mapping=record.field_mapping,
            slot_schema=record.slot_schema,
            graph_build_hints=record.graph_build_hints,
            resume_policy=record.resume_policy,
        )
