from __future__ import annotations

from functools import lru_cache

from admin_api.dependencies import get_intent_repository
from router_api.sse.broker import EventBroker
from router_core.intent_catalog import RepositoryIntentCatalog
from router_core.orchestrator import RouterOrchestrator


@lru_cache
def get_event_broker() -> EventBroker:
    return EventBroker()


@lru_cache
def get_orchestrator() -> RouterOrchestrator:
    return RouterOrchestrator(
        publish_event=get_event_broker().publish,
        intent_catalog=RepositoryIntentCatalog(get_intent_repository()),
    )
