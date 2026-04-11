from __future__ import annotations

from router_service.api import dependencies as service_dependencies


RouterRuntime = service_dependencies.RouterRuntime
get_event_broker = service_dependencies.get_event_broker
get_event_broker_v2 = service_dependencies.get_event_broker_v2
get_intent_catalog = service_dependencies.get_intent_catalog
get_llm_client = service_dependencies.get_llm_client
get_orchestrator = service_dependencies.get_orchestrator
get_orchestrator_v2 = service_dependencies.get_orchestrator_v2
get_router_runtime = service_dependencies.get_router_runtime


def get_settings():
    return service_dependencies.get_settings()


def get_intent_repository():
    return service_dependencies.get_intent_repository()


def _build_llm_client():
    return service_dependencies._build_llm_client()


def build_router_runtime():
    original_get_settings = service_dependencies.get_settings
    original_get_intent_repository = service_dependencies.get_intent_repository
    original_build_llm_client = service_dependencies._build_llm_client
    service_dependencies.get_settings = get_settings
    service_dependencies.get_intent_repository = get_intent_repository
    service_dependencies._build_llm_client = _build_llm_client
    try:
        return service_dependencies.build_router_runtime()
    finally:
        service_dependencies.get_settings = original_get_settings
        service_dependencies.get_intent_repository = original_get_intent_repository
        service_dependencies._build_llm_client = original_build_llm_client


def close_router_runtime(runtime):
    return service_dependencies.close_router_runtime(runtime)


def run_intent_catalog_refresh(*args, **kwargs):
    return service_dependencies.run_intent_catalog_refresh(*args, **kwargs)


__all__ = [
    "RouterRuntime",
    "_build_llm_client",
    "build_router_runtime",
    "close_router_runtime",
    "get_event_broker",
    "get_event_broker_v2",
    "get_intent_catalog",
    "get_intent_repository",
    "get_llm_client",
    "get_orchestrator",
    "get_orchestrator_v2",
    "get_router_runtime",
    "get_settings",
    "run_intent_catalog_refresh",
]
