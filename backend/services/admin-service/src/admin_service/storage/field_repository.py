from __future__ import annotations

from abc import ABC, abstractmethod

from intent_registry_contracts.models import IntentFieldDefinition, IntentFieldRecord


class IntentFieldRepositoryError(Exception):
    """Base field repository error."""


class IntentFieldAlreadyExistsError(IntentFieldRepositoryError):
    """Raised when creating a field with an existing field_code."""


class IntentFieldNotFoundError(IntentFieldRepositoryError):
    """Raised when a field cannot be found."""


class IntentFieldRepository(ABC):
    @abstractmethod
    def list_fields(self) -> list[IntentFieldRecord]:
        raise NotImplementedError

    @abstractmethod
    def get_field(self, field_code: str) -> IntentFieldRecord:
        raise NotImplementedError

    @abstractmethod
    def create_field(self, payload: IntentFieldDefinition) -> IntentFieldRecord:
        raise NotImplementedError

    @abstractmethod
    def update_field(self, field_code: str, payload: IntentFieldDefinition) -> IntentFieldRecord:
        raise NotImplementedError

    @abstractmethod
    def delete_field(self, field_code: str) -> None:
        raise NotImplementedError
