from __future__ import annotations

from abc import ABC, abstractmethod

from router_service.models.intent import IntentPayload, IntentRecord, IntentStatus


class IntentRepositoryError(Exception):
    """Base repository error."""


class IntentAlreadyExistsError(IntentRepositoryError):
    """Raised when creating an intent with an existing intent_code."""


class IntentNotFoundError(IntentRepositoryError):
    """Raised when an intent cannot be found."""


class IntentRepository(ABC):
    """Abstract repository interface for admin-managed intent definitions."""

    @abstractmethod
    def list_intents(self, status: IntentStatus | None = None) -> list[IntentRecord]:
        """List intents, optionally filtered by status."""
        raise NotImplementedError

    @abstractmethod
    def get_intent(self, intent_code: str) -> IntentRecord:
        """Fetch a single intent by intent code."""
        raise NotImplementedError

    @abstractmethod
    def create_intent(self, payload: IntentPayload) -> IntentRecord:
        """Create a new intent definition."""
        raise NotImplementedError

    @abstractmethod
    def update_intent(self, intent_code: str, payload: IntentPayload) -> IntentRecord:
        """Replace an existing intent definition."""
        raise NotImplementedError

    @abstractmethod
    def delete_intent(self, intent_code: str) -> None:
        """Delete an intent definition."""
        raise NotImplementedError
