from __future__ import annotations

from functools import lru_cache

from config.settings import Settings
from persistence.in_memory_intent_repository import InMemoryIntentRepository
from persistence.intent_repository import IntentRepository
from persistence.postgres_intent_repository import PostgresIntentRepository


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_intent_repository() -> IntentRepository:
    settings = get_settings()
    if settings.repository_backend == "memory":
        return InMemoryIntentRepository()
    if settings.repository_backend == "postgres":
        if not settings.postgres_dsn:
            raise RuntimeError("ADMIN_POSTGRES_DSN is required when backend=postgres")
        return PostgresIntentRepository(settings.postgres_dsn)
    raise RuntimeError(f"Unsupported repository backend: {settings.repository_backend}")

