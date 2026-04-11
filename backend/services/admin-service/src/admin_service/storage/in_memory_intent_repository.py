from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from intent_registry_contracts.models import IntentPayload, IntentRecord, IntentStatus
from admin_service.storage.intent_repository import (
    IntentAlreadyExistsError,
    IntentNotFoundError,
    IntentRepository,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryIntentRepository(IntentRepository):
    def __init__(self) -> None:
        self._store: dict[str, IntentRecord] = {}
        self._lock = RLock()

    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        with self._lock:
            intents = list(self._store.values())
            if status is None:
                return intents
            return [intent for intent in intents if intent.status == status]

    def get_intent(self, intent_code: str) -> IntentRecord:
        with self._lock:
            intent = self._store.get(intent_code)
            if intent is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            return intent

    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        with self._lock:
            if payload.intent_code in self._store:
                raise IntentAlreadyExistsError(f"Intent already exists: {payload.intent_code}")
            now = utcnow()
            record = IntentRecord(**payload.model_dump(), created_at=now, updated_at=now)
            self._store[payload.intent_code] = record
            return record

    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        with self._lock:
            current = self._store.get(intent_code)
            if current is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            if payload.intent_code != intent_code and payload.intent_code in self._store:
                raise IntentAlreadyExistsError(f"Intent already exists: {payload.intent_code}")
            now = utcnow()
            updated = IntentRecord(
                **payload.model_dump(),
                created_at=current.created_at,
                updated_at=now,
            )
            if payload.intent_code != intent_code:
                del self._store[intent_code]
            self._store[payload.intent_code] = updated
            return updated

    def delete_intent(self, intent_code: str) -> None:
        with self._lock:
            if intent_code not in self._store:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            del self._store[intent_code]

