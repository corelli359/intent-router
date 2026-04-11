from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock

from intent_registry_contracts.models import IntentFieldDefinition, IntentFieldRecord
from admin_service.storage.field_repository import (
    IntentFieldAlreadyExistsError,
    IntentFieldNotFoundError,
    IntentFieldRepository,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryIntentFieldRepository(IntentFieldRepository):
    def __init__(self) -> None:
        self._store: dict[str, IntentFieldRecord] = {}
        self._lock = RLock()

    def list_fields(self) -> list[IntentFieldRecord]:
        with self._lock:
            return list(self._store.values())

    def get_field(self, field_code: str) -> IntentFieldRecord:
        with self._lock:
            field = self._store.get(field_code)
            if field is None:
                raise IntentFieldNotFoundError(f"Field not found: {field_code}")
            return field

    def create_field(self, payload: IntentFieldDefinition) -> IntentFieldRecord:
        with self._lock:
            if payload.field_code in self._store:
                raise IntentFieldAlreadyExistsError(f"Field already exists: {payload.field_code}")
            now = utcnow()
            record = IntentFieldRecord(**payload.model_dump(), created_at=now, updated_at=now)
            self._store[payload.field_code] = record
            return record

    def update_field(self, field_code: str, payload: IntentFieldDefinition) -> IntentFieldRecord:
        with self._lock:
            current = self._store.get(field_code)
            if current is None:
                raise IntentFieldNotFoundError(f"Field not found: {field_code}")
            if payload.field_code != field_code and payload.field_code in self._store:
                raise IntentFieldAlreadyExistsError(f"Field already exists: {payload.field_code}")
            now = utcnow()
            updated = IntentFieldRecord(**payload.model_dump(), created_at=current.created_at, updated_at=now)
            if payload.field_code != field_code:
                del self._store[field_code]
            self._store[payload.field_code] = updated
            return updated

    def delete_field(self, field_code: str) -> None:
        with self._lock:
            if field_code not in self._store:
                raise IntentFieldNotFoundError(f"Field not found: {field_code}")
            del self._store[field_code]
