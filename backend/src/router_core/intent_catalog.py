from __future__ import annotations

from models.intent import IntentRecord
from persistence.intent_repository import IntentRepository
from router_core.demo_intents import DEMO_INTENTS
from router_core.domain import IntentDefinition


class RepositoryIntentCatalog:
    def __init__(self, repository: IntentRepository) -> None:
        self.repository = repository

    def list_active(self) -> list[IntentDefinition]:
        records = self.repository.list_intents()
        if not records:
            return list(DEMO_INTENTS)
        return [self._to_definition(record) for record in records if record.status.value == "active"]

    def priorities(self) -> dict[str, int]:
        return {intent.intent_code: intent.dispatch_priority for intent in self.list_active()}

    def _to_definition(self, record: IntentRecord) -> IntentDefinition:
        return IntentDefinition(
            intent_code=record.intent_code,
            name=record.name,
            description=record.description,
            examples=record.examples,
            agent_url=record.agent_url,
            status=record.status.value,
            dispatch_priority=record.dispatch_priority,
            request_schema=record.request_schema,
            field_mapping=record.field_mapping,
            resume_policy=record.resume_policy,
        )

