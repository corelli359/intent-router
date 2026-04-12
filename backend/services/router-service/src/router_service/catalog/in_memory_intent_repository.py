from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from router_service.models.intent import IntentPayload, IntentRecord, IntentStatus
from router_service.catalog.intent_repository import (
    IntentAlreadyExistsError,
    IntentNotFoundError,
    IntentRepository,
)


def utcnow() -> datetime:
    """Return the current UTC timestamp for repository records."""
    return datetime.now(timezone.utc)


class InMemoryIntentRepository(IntentRepository):
    """Thread-safe in-memory intent repository used for local development and tests."""

    def __init__(self) -> None:
        """Initialize the in-memory store and its lock."""
        self._store: dict[str, IntentRecord] = {}
        self._lock = RLock()

    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        """Return all intents, optionally filtered by status."""
        with self._lock:
            intents = list(self._store.values())
            if status is None:
                return intents
            return [intent for intent in intents if intent.status == status]

    def get_intent(self, intent_code: str) -> IntentRecord:
        """Return one intent or raise when it does not exist."""
        with self._lock:
            intent = self._store.get(intent_code)
            if intent is None:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            return intent

    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        """Insert a new intent record into the in-memory store."""
        with self._lock:
            if payload.intent_code in self._store:
                raise IntentAlreadyExistsError(f"Intent already exists: {payload.intent_code}")
            now = utcnow()
            record = IntentRecord(**payload.model_dump(), created_at=now, updated_at=now)
            self._store[payload.intent_code] = record
            return record

    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        """Update or rename an existing intent record."""
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
        """Delete an intent record from the in-memory store."""
        with self._lock:
            if intent_code not in self._store:
                raise IntentNotFoundError(f"Intent not found: {intent_code}")
            del self._store[intent_code]
