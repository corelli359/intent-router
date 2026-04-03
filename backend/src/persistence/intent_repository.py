from __future__ import annotations

from abc import ABC, abstractmethod

from models.intent import IntentPayload, IntentRecord, IntentStatus


class IntentRepositoryError(Exception):
    """Base repository error."""


class IntentAlreadyExistsError(IntentRepositoryError):
    """Raised when creating an intent with an existing intent_code."""


class IntentNotFoundError(IntentRepositoryError):
    """Raised when an intent cannot be found."""


class IntentRepository(ABC):
    @abstractmethod
    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        raise NotImplementedError

    @abstractmethod
    def get_intent(self, intent_code: str) -> IntentRecord:
        raise NotImplementedError

    @abstractmethod
    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        raise NotImplementedError

    @abstractmethod
    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        raise NotImplementedError

    @abstractmethod
    def delete_intent(self, intent_code: str) -> None:
        raise NotImplementedError

