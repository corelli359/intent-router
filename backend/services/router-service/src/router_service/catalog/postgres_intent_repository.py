from __future__ import annotations

from router_service.catalog.sql_intent_repository import DatabaseIntentRepository


class PostgresIntentRepository(DatabaseIntentRepository):
    """Compatibility alias for deployments that want an explicit Postgres repository name."""
