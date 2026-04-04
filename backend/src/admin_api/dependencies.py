from __future__ import annotations

from functools import lru_cache

from config.settings import Settings
from persistence.in_memory_intent_repository import InMemoryIntentRepository
from persistence.intent_repository import IntentRepository
from persistence.sql_intent_repository import DatabaseIntentRepository


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
