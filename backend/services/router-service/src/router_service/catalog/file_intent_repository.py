from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import ValidationError

from router_service.catalog.intent_repository import (
    IntentNotFoundError,
    IntentRepository,
    IntentRepositoryError,
    IntentRepositoryReadOnlyError,
)
from router_service.models.intent import IntentPayload, IntentRecord, IntentStatus


class FileIntentRepository(IntentRepository):
    """Read-only intent repository backed by a JSON file on disk."""

    def __init__(self, catalog_path: str | Path) -> None:
        """Store the configured catalog path for later refresh reads."""
        self.catalog_path = Path(catalog_path).expanduser()
        self._lock = RLock()

    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        """Load intents from disk and optionally filter by lifecycle status."""
        with self._lock:
            intents = self._load_records()
            if status is None:
                return intents
            return [intent for intent in intents if intent.status == status]

    def get_intent(self, intent_code: str) -> IntentRecord:
        """Return one intent by code from the current file snapshot."""
        with self._lock:
            for intent in self._load_records():
                if intent.intent_code == intent_code:
                    return intent
        raise IntentNotFoundError(f"Intent not found: {intent_code}")

    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        """Reject writes because file-backed catalogs are configured externally."""
        raise IntentRepositoryReadOnlyError(
            f"Intent catalog file is read-only: {self.catalog_path}"
        )

    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        """Reject writes because file-backed catalogs are configured externally."""
        raise IntentRepositoryReadOnlyError(
            f"Intent catalog file is read-only: {self.catalog_path}"
        )

    def delete_intent(self, intent_code: str) -> None:
        """Reject writes because file-backed catalogs are configured externally."""
        raise IntentRepositoryReadOnlyError(
            f"Intent catalog file is read-only: {self.catalog_path}"
        )

    def _load_records(self) -> list[IntentRecord]:
        """Read and validate the full file-backed intent catalog."""
        payload = self._load_payload()
        intents = self._extract_intents(payload)
        records: list[IntentRecord] = []
        seen_codes: set[str] = set()
        for index, item in enumerate(intents):
            if not isinstance(item, dict):
                raise IntentRepositoryError(
                    f"Intent catalog item at index {index} must be a JSON object"
                )
            try:
                record = IntentRecord.model_validate(item)
            except ValidationError as exc:
                raise IntentRepositoryError(
                    f"Invalid intent catalog item at index {index}: {exc}"
                ) from exc
            if record.intent_code in seen_codes:
                raise IntentRepositoryError(
                    f"Duplicate intent_code in catalog file: {record.intent_code}"
                )
            seen_codes.add(record.intent_code)
            records.append(record)
        return records

    def _load_payload(self) -> Any:
        """Read the raw JSON document from disk."""
        if not self.catalog_path.is_file():
            raise IntentRepositoryError(
                f"Intent catalog file does not exist: {self.catalog_path}"
            )
        try:
            raw_text = self.catalog_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise IntentRepositoryError(
                f"Failed to read intent catalog file: {self.catalog_path}"
            ) from exc
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise IntentRepositoryError(
                f"Intent catalog file must contain valid JSON: {self.catalog_path}"
            ) from exc

    def _extract_intents(self, payload: Any) -> list[Any]:
        """Normalize supported JSON shapes into one list of intent objects."""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            intents = payload.get("intents")
            if isinstance(intents, list):
                return intents
        raise IntentRepositoryError(
            "Intent catalog file must be a JSON array or an object containing an 'intents' array"
        )
