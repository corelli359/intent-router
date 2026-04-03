from __future__ import annotations

from models.intent import IntentPayload, IntentRecord, IntentStatus
from persistence.intent_repository import IntentRepository


class PostgresIntentRepository(IntentRepository):
    """
    Placeholder implementation to preserve repository boundaries.
    A concrete SQLAlchemy/asyncpg implementation can be dropped in later.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        raise NotImplementedError("Postgres repository is not implemented yet.")

    def get_intent(self, intent_code: str) -> IntentRecord:
        raise NotImplementedError("Postgres repository is not implemented yet.")

    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        raise NotImplementedError("Postgres repository is not implemented yet.")

    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        raise NotImplementedError("Postgres repository is not implemented yet.")

    def delete_intent(self, intent_code: str) -> None:
        raise NotImplementedError("Postgres repository is not implemented yet.")

