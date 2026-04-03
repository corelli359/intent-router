from __future__ import annotations

from functools import lru_cache

from admin_api.dependencies import get_intent_repository
from admin_api.dependencies import get_settings
from router_api.sse.broker import EventBroker
from router_core.agent_client import StreamingAgentClient
from router_core.intent_catalog import RepositoryIntentCatalog
from router_core.llm_client import LangChainLLMClient
from router_core.orchestrator import RouterOrchestrator
from router_core.recognizer import LLMIntentRecognizer, SimpleIntentRecognizer


@lru_cache
def get_event_broker() -> EventBroker:
    return EventBroker()


@lru_cache
def get_llm_client() -> LangChainLLMClient | None:
    settings = get_settings()
    if not settings.llm_connection_ready or settings.default_llm_model is None:
        return None
    return LangChainLLMClient(
        base_url=settings.llm_api_base_url or "",
        api_key=settings.llm_api_key,
        default_model=settings.default_llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        extra_headers=settings.llm_headers,
        structured_output_method=settings.llm_structured_output_method,
    )


@lru_cache
def get_orchestrator() -> RouterOrchestrator:
    settings = get_settings()
    llm_client = get_llm_client()
    recognizer = (
        LLMIntentRecognizer(
            llm_client,
            model=settings.llm_recognizer_model or settings.llm_model,
            fallback=SimpleIntentRecognizer(),
        )
        if settings.recognizer_backend == "llm" and llm_client is not None
        else SimpleIntentRecognizer()
    )
    return RouterOrchestrator(
        publish_event=get_event_broker().publish,
        intent_catalog=RepositoryIntentCatalog(get_intent_repository()),
        recognizer=recognizer,
        agent_client=StreamingAgentClient(
            llm_client=llm_client,
            llm_model=settings.llm_agent_model or settings.llm_model,
            enable_llm_for_mock_scheme=settings.enable_llm_for_mock_agent,
            http_timeout_seconds=settings.agent_http_timeout_seconds,
        ),
    )
