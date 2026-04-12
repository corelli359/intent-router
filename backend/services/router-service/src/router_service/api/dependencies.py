from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import logging

from fastapi import Request

from router_service.catalog.in_memory_intent_repository import InMemoryIntentRepository
from router_service.catalog.intent_repository import IntentRepository
from router_service.catalog.sql_intent_repository import DatabaseIntentRepository
from router_service.api.sse.broker import EventBroker
from router_service.core.agent_client import StreamingAgentClient
from router_service.core.intent_catalog import RepositoryIntentCatalog
from router_service.core.llm_client import LangChainLLMClient
from router_service.core.prompt_templates import (
    DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT,
    DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT,
    DEFAULT_LEAF_ROUTER_HUMAN_PROMPT,
    DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT,
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
)
from router_service.core.recognizer import LLMIntentRecognizer, NullIntentRecognizer
from router_service.core.graph_builder import LLMIntentGraphBuilder
from router_service.core.graph_orchestrator import GraphRouterOrchestrator, GraphRouterOrchestratorConfig
from router_service.core.graph_planner import (
    BasicTurnInterpreter,
    LLMGraphTurnInterpreter,
    LLMIntentGraphPlanner,
    SequentialIntentGraphPlanner,
)
from router_service.core.recommendation_router import (
    LLMProactiveRecommendationRouter,
    NullProactiveRecommendationRouter,
)
from router_service.core.domain_router import DomainRouter
from router_service.core.hierarchical_intent_recognizer import HierarchicalIntentRecognizer
from router_service.core.leaf_intent_router import LeafIntentRouter
from router_service.core.slot_extractor import SlotExtractor
from router_service.core.slot_validator import SlotValidator
from router_service.core.understanding_validator import UnderstandingValidator
from router_service.settings import Settings


logger = logging.getLogger(__name__)


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


@dataclass(slots=True)
class RouterRuntime:
    event_broker: EventBroker
    llm_client: LangChainLLMClient | None
    intent_catalog: RepositoryIntentCatalog
    agent_client: StreamingAgentClient
    orchestrator: GraphRouterOrchestrator


def _warn_null_recognizer(*, recognizer_backend: str, llm_available: bool) -> NullIntentRecognizer:
    logger.warning(
        "Router intent recognition requires LLM semantics "
        "(backend=%s, llm_available=%s). Falling back to NullIntentRecognizer "
        "so unmatched requests can be handled by the fallback intent/agent.",
        recognizer_backend,
        llm_available,
    )
    return NullIntentRecognizer()


def build_router_runtime() -> RouterRuntime:
    settings = get_settings()
    event_broker = EventBroker(
        heartbeat_interval_seconds=settings.router_sse_heartbeat_seconds,
        max_idle_seconds=settings.router_sse_max_idle_seconds,
    )
    llm_client = _build_llm_client()
    intent_catalog = RepositoryIntentCatalog(get_intent_repository())
    baseline_recognizer = (
        LLMIntentRecognizer(
            llm_client,
            model=settings.llm_recognizer_model or settings.llm_model,
            fallback=NullIntentRecognizer(),
            system_prompt_template=settings.llm_recognizer_system_prompt_template or DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
            human_prompt_template=settings.llm_recognizer_human_prompt_template or DEFAULT_RECOGNIZER_HUMAN_PROMPT,
        )
        if llm_client is not None
        else _warn_null_recognizer(
            recognizer_backend=settings.recognizer_backend,
            llm_available=False,
        )
    )
    recognizer = baseline_recognizer
    if settings.router_v2_understanding_mode == "hierarchical" and llm_client is not None:
        domain_recognizer = LLMIntentRecognizer(
            llm_client,
            model=settings.llm_recognizer_model or settings.llm_model,
            fallback=NullIntentRecognizer(),
            system_prompt_template=DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT,
            human_prompt_template=DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT,
        )
        leaf_recognizer = LLMIntentRecognizer(
            llm_client,
            model=settings.llm_recognizer_model or settings.llm_model,
            fallback=NullIntentRecognizer(),
            system_prompt_template=DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT,
            human_prompt_template=DEFAULT_LEAF_ROUTER_HUMAN_PROMPT,
        )
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=intent_catalog,
            domain_router=DomainRouter(domain_recognizer),
            leaf_router=LeafIntentRouter(leaf_recognizer),
            fallback=baseline_recognizer,
        )
    agent_client = StreamingAgentClient(http_timeout_seconds=settings.agent_http_timeout_seconds)
    understanding_validator = UnderstandingValidator(
        slot_extractor=SlotExtractor(
            llm_client=llm_client,
            model=settings.llm_model,
        ),
        slot_validator=SlotValidator(),
    )
    orchestrator = GraphRouterOrchestrator(
        publish_event=event_broker.publish,
        intent_catalog=intent_catalog,
        recognizer=recognizer,
        graph_builder=(
            LLMIntentGraphBuilder(
                llm_client,
                model=settings.llm_model,
                fallback_recognizer=recognizer,
                fallback_planner=SequentialIntentGraphPlanner(),
            )
            if llm_client is not None
            and settings.router_v2_graph_build_mode == "unified"
            and settings.router_v2_understanding_mode != "hierarchical"
            else None
        ),
        planner=(
            LLMIntentGraphPlanner(
                llm_client,
                model=settings.llm_model,
                fallback=SequentialIntentGraphPlanner(),
            )
            if llm_client is not None
            else SequentialIntentGraphPlanner()
        ),
        turn_interpreter=(
            LLMGraphTurnInterpreter(
                llm_client,
                model=settings.llm_model,
                fallback=BasicTurnInterpreter(),
            )
            if llm_client is not None
            else BasicTurnInterpreter()
        ),
        recommendation_router=(
            LLMProactiveRecommendationRouter(
                llm_client,
                model=settings.llm_model,
                fallback=NullProactiveRecommendationRouter(),
            )
            if llm_client is not None
            else NullProactiveRecommendationRouter()
        ),
        agent_client=agent_client,
        config=GraphRouterOrchestratorConfig(
            intent_switch_threshold=settings.router_intent_switch_threshold,
            agent_timeout_seconds=settings.router_agent_timeout_seconds,
        ),
        understanding_validator=understanding_validator,
    )
    return RouterRuntime(
        event_broker=event_broker,
        llm_client=llm_client,
        intent_catalog=intent_catalog,
        agent_client=agent_client,
        orchestrator=orchestrator,
    )


def _build_llm_client() -> LangChainLLMClient | None:
    settings = get_settings()
    if not settings.llm_connection_ready or settings.default_llm_model is None:
        return None
    return LangChainLLMClient(
        base_url=settings.llm_api_base_url or "",
        api_key=settings.llm_api_key,
        default_model=settings.default_llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        rate_limit_max_retries=getattr(settings, "llm_rate_limit_max_retries", 2),
        rate_limit_retry_delay_seconds=getattr(settings, "llm_rate_limit_retry_delay_seconds", 2.0),
        extra_headers=settings.llm_headers,
        structured_output_method=settings.llm_structured_output_method,
    )


def get_router_runtime(request: Request) -> RouterRuntime:
    runtime = getattr(request.app.state, "router_runtime", None)
    if runtime is None:
        runtime = build_router_runtime()
        request.app.state.router_runtime = runtime
    return runtime


def get_event_broker(request: Request) -> EventBroker:
    return get_router_runtime(request).event_broker


def get_llm_client(request: Request) -> LangChainLLMClient | None:
    return get_router_runtime(request).llm_client


def get_intent_catalog(request: Request) -> RepositoryIntentCatalog:
    return get_router_runtime(request).intent_catalog


def get_orchestrator(request: Request) -> GraphRouterOrchestrator:
    return get_router_runtime(request).orchestrator


def get_event_broker_v2(request: Request) -> EventBroker:
    return get_event_broker(request)


def get_orchestrator_v2(request: Request) -> GraphRouterOrchestrator:
    return get_orchestrator(request)


async def close_router_runtime(runtime: RouterRuntime) -> None:
    await runtime.agent_client.close()


async def run_intent_catalog_refresh(
    stop_event: asyncio.Event,
    *,
    catalog: RepositoryIntentCatalog,
    refresh_interval_seconds: float,
) -> None:
    consecutive_failures = 0
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(catalog.refresh_now)
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            logger.warning("intent catalog refresh failed: %s", exc)
            if consecutive_failures >= 3:
                logger.error(
                    "intent catalog refresh has failed %s consecutive times",
                    consecutive_failures,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=refresh_interval_seconds)
        except asyncio.TimeoutError:
            continue
