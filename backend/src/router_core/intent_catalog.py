from __future__ import annotations

import time
from threading import RLock
from typing import Callable

from models.intent import IntentRecord, IntentStatus
from persistence.intent_repository import IntentRepository
from router_core.demo_intents import DEMO_INTENTS
from router_core.domain import IntentDefinition


class RepositoryIntentCatalog:
    def __init__(
        self,
        repository: IntentRepository,
        *,
        refresh_interval_seconds: float = 5.0,
        use_demo_intents: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.repository = repository
        self.refresh_interval_seconds = refresh_interval_seconds
        self.use_demo_intents = use_demo_intents
        self.clock = clock or time.monotonic
        self._lock = RLock()
        self._active_cache: list[IntentDefinition] = []
        self._fallback_cache: IntentDefinition | None = None
        self._priorities_cache: dict[str, int] = {}
        self._last_refresh_at: float | None = None

    def list_active(self) -> list[IntentDefinition]:
        self._refresh_if_needed()
        with self._lock:
            return list(self._active_cache)

    def priorities(self) -> dict[str, int]:
        self._refresh_if_needed()
        with self._lock:
            return dict(self._priorities_cache)

    def get_fallback_intent(self) -> IntentDefinition | None:
        self._refresh_if_needed()
        with self._lock:
            if self._fallback_cache is None:
                return None
            return self._fallback_cache.model_copy(deep=True)

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

        if not routable_intents and self.use_demo_intents:
            routable_intents = list(DEMO_INTENTS)

        fallback_intents.sort(key=lambda intent: intent.dispatch_priority, reverse=True)
        fallback_intent = fallback_intents[0] if fallback_intents else None

        with self._lock:
            self._active_cache = routable_intents
            self._fallback_cache = fallback_intent
            self._priorities_cache = {
                intent.intent_code: intent.dispatch_priority
                for intent in [*routable_intents, *fallback_intents]
            }
            self._last_refresh_at = self.clock()
            return list(self._active_cache)

    def _refresh_if_needed(self) -> None:
        with self._lock:
            should_refresh = self._last_refresh_at is None or (
                self.clock() - self._last_refresh_at >= self.refresh_interval_seconds
            )
        if should_refresh:
            self.refresh_now()

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
            resume_policy=record.resume_policy,
        )
