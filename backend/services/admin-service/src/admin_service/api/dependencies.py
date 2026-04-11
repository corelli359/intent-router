from __future__ import annotations

from functools import lru_cache

from admin_service.settings import Settings
from admin_service.storage.field_repository import IntentFieldRepository
from admin_service.storage.in_memory_field_repository import InMemoryIntentFieldRepository
from admin_service.storage.in_memory_intent_repository import InMemoryIntentRepository
from admin_service.storage.intent_repository import IntentRepository
from admin_service.storage.sql_field_repository import DatabaseIntentFieldRepository
from admin_service.storage.sql_intent_repository import DatabaseIntentRepository


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_intent_repository() -> IntentRepository:
    settings = get_settings()
    if settings.repository_backend == "memory":
        return InMemoryIntentRepository()
    if settings.repository_backend in {"database", "postgres"}:
        if not settings.database_url:
            raise RuntimeError("ADMIN_DATABASE_URL is required when backend=database")
        return DatabaseIntentRepository(settings.database_url)
    raise RuntimeError(f"Unsupported repository backend: {settings.repository_backend}")


@lru_cache(maxsize=1)
def get_field_repository() -> IntentFieldRepository:
    settings = get_settings()
    if settings.repository_backend == "memory":
        return InMemoryIntentFieldRepository()
    if settings.repository_backend in {"database", "postgres"}:
        if not settings.database_url:
            raise RuntimeError("ADMIN_DATABASE_URL is required when backend=database")
        return DatabaseIntentFieldRepository(settings.database_url)
    raise RuntimeError(f"Unsupported repository backend: {settings.repository_backend}")
