from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import httpx
import logging

from fastapi import Request

from router_service.catalog.file_intent_repository import FileIntentRepository
from router_service.catalog.in_memory_intent_repository import InMemoryIntentRepository
from router_service.catalog.intent_repository import IntentRepository
from router_service.catalog.sql_intent_repository import DatabaseIntentRepository
from router_service.api.sse.broker import EventBroker
from router_service.core.support.agent_barrier import BarrierAgentClient
from router_service.core.support.agent_client import AgentClient, StreamingAgentClient
from router_service.core.support.intent_catalog import RepositoryIntentCatalog
from router_service.core.support.llm_client import JsonLLMClient, LangChainLLMClient
from router_service.core.support.perf_llm_client import FastPerfLLMClient
from router_service.core.support.jwt_utils import AuthHTTPClient
from router_service.core.support.memory_store import LongTermMemoryStore
from router_service.core.skill_runtime.runtime import SkillRuntimeController
from router_service.core.prompts.prompt_templates import (
    DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT,
    DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT,
    DEFAULT_LEAF_ROUTER_HUMAN_PROMPT,
    DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT,
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
    DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT,
    DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT,
)
from router_service.core.recognition.recognizer import (
    LLMIntentRecognizer,
    NullIntentRecognizer,
)
from router_service.core.graph.builder import LLMIntentGraphBuilder
from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.graph.orchestrator import GraphRouterOrchestrator, GraphRouterOrchestratorConfig
from router_service.core.graph.planner import (
    BasicTurnInterpreter,
    LLMGraphTurnInterpreter,
    LLMIntentGraphPlanner,
    SequentialIntentGraphPlanner,
)
from router_service.core.graph.recommendation_router import (
    LLMProactiveRecommendationRouter,
    NullProactiveRecommendationRouter,
)
from router_service.core.recognition.domain_router import DomainRouter
from router_service.core.recognition.hierarchical_intent_recognizer import HierarchicalIntentRecognizer
from router_service.core.recognition.leaf_intent_router import LeafIntentRouter
from router_service.core.slots.extractor import SlotExtractor
from router_service.core.slots.validator import SlotValidator
from router_service.core.slots.understanding_validator import UnderstandingValidator
from router_service.settings import Settings


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached router settings object."""
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_intent_repository() -> IntentRepository:
    """Build and cache the configured intent repository implementation."""
    settings = get_settings()
    if settings.repository_backend == "memory":
        return InMemoryIntentRepository()
    if settings.repository_backend == "file":
        if not settings.router_intent_catalog_file:
            raise RuntimeError("ROUTER_INTENT_CATALOG_FILE is required when backend=file")
        return FileIntentRepository(
            settings.router_intent_catalog_file,
            field_catalog_path=settings.router_intent_field_catalog_file,
            slot_schema_path=settings.router_intent_slot_schema_file,
            graph_build_hints_path=settings.router_intent_graph_build_hints_file,
        )
    if settings.repository_backend in {"database", "postgres"}:
        if not settings.database_url:
            raise RuntimeError(
                "ROUTER_INTENT_CATALOG_DATABASE_URL or ADMIN_DATABASE_URL is required when backend=database"
            )
        return DatabaseIntentRepository(settings.database_url)
    raise RuntimeError(f"Unsupported repository backend: {settings.repository_backend}")


@dataclass(slots=True)
class RouterRuntime:
    """Container for long-lived runtime dependencies stored on the FastAPI app."""

    event_broker: EventBroker
    llm_client: JsonLLMClient | None
    intent_catalog: RepositoryIntentCatalog
    agent_client: AgentClient
    orchestrator: GraphRouterOrchestrator
    session_store: GraphSessionStore
    skill_runtime: SkillRuntimeController


def _warn_null_recognizer(*, recognizer_backend: str, llm_available: bool) -> NullIntentRecognizer:
    """Log and return a no-op recognizer when semantic routing is unavailable."""
    logger.warning(
        "Router intent recognition requires LLM semantics "
        "(backend=%s, llm_available=%s). Falling back to NullIntentRecognizer "
        "so unmatched requests can be handled by the fallback intent/agent.",
        recognizer_backend,
        llm_available,
    )
    return NullIntentRecognizer()


def build_router_runtime() -> RouterRuntime:
    """Assemble the full router runtime from repository, LLM, graph, and agent components."""
    settings = get_settings()
    planning_policy = getattr(settings, "router_v2_planning_policy", "auto")
    logger.info(
        "Building router runtime dependencies (catalog_backend=%s, llm_model=%s, recognizer_model=%s)",
        getattr(settings, "repository_backend", "memory"),
        settings.llm_model,
        settings.llm_recognizer_model or settings.llm_model,
    )
    session_store = GraphSessionStore(
        long_term_memory=LongTermMemoryStore(
            fact_limit=getattr(settings, "router_long_term_memory_fact_limit", 100)
        )
    )
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
            system_prompt_template=DEFAULT_DOMAIN_ROUTER_SYSTEM_PROMPT,
            human_prompt_template=DEFAULT_DOMAIN_ROUTER_HUMAN_PROMPT,
        )
        leaf_recognizer = LLMIntentRecognizer(
            llm_client,
            model=settings.llm_recognizer_model or settings.llm_model,
            system_prompt_template=DEFAULT_LEAF_ROUTER_SYSTEM_PROMPT,
            human_prompt_template=DEFAULT_LEAF_ROUTER_HUMAN_PROMPT,
        )
        recognizer = HierarchicalIntentRecognizer(
            intent_catalog=intent_catalog,
            domain_router=DomainRouter(domain_recognizer),
            leaf_router=LeafIntentRouter(leaf_recognizer),
            fallback=baseline_recognizer,
        )
    if getattr(settings, "router_agent_barrier_enabled", False):
        logger.info("Router agent barrier enabled; downstream agent HTTP calls will be blocked")
        agent_client: AgentClient = BarrierAgentClient()
    else:
        agent_client = StreamingAgentClient(http_timeout_seconds=settings.agent_http_timeout_seconds)
    understanding_validator = UnderstandingValidator(
        slot_extractor=SlotExtractor(
            llm_client=llm_client,
            model=settings.llm_model,
            system_prompt_template=(
                getattr(settings, "llm_slot_extractor_system_prompt_template", None)
                or DEFAULT_SLOT_EXTRACTOR_SYSTEM_PROMPT
            ),
            human_prompt_template=(
                getattr(settings, "llm_slot_extractor_human_prompt_template", None)
                or DEFAULT_SLOT_EXTRACTOR_HUMAN_PROMPT
            ),
        ),
        slot_validator=SlotValidator(),
    )
    orchestrator = GraphRouterOrchestrator(
        publish_event=event_broker.publish,
        session_store=session_store,
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
            and planning_policy == "always"
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
            memory_recall_limit=getattr(settings, "router_memory_recall_limit", 20),
            session_task_limit=getattr(settings, "router_session_max_tasks", 5),
            session_business_limit=getattr(settings, "router_session_max_businesses", 5),
            max_drain_iterations=getattr(settings, "router_drain_max_iterations", None),
            drain_iteration_multiplier=getattr(settings, "router_drain_iteration_multiplier", 3),
            drain_iteration_floor=getattr(settings, "router_drain_iteration_floor", 8),
        ),
        planning_policy=planning_policy,
        understanding_validator=understanding_validator,
    )
    return RouterRuntime(
        event_broker=event_broker,
        llm_client=llm_client,
        intent_catalog=intent_catalog,
        agent_client=agent_client,
        orchestrator=orchestrator,
        session_store=session_store,
        skill_runtime=SkillRuntimeController.from_spec_root(
            getattr(settings, "router_v4_skill_root", None)
        ),
    )


def _build_llm_client() -> JsonLLMClient | None:
    """Create the shared LLM client when the required connection settings are present."""
    settings = get_settings()
    if getattr(settings, "llm_fast_fake_enabled", False):
        logger.info(
            "Router LLM perf stub enabled (model=%s)",
            settings.default_llm_model,
        )
        return FastPerfLLMClient(default_model=settings.default_llm_model or "fake-router-llm")
    if not settings.llm_connection_ready or settings.default_llm_model is None:
        logger.info(
            "Router LLM client disabled (base_url=%s, model=%s)",
            bool(settings.llm_api_base_url),
            settings.default_llm_model,
        )
        return None
    logger.info(
        "Router LLM client enabled (base_url=%s, model=%s, structured_output_method=%s, auth_http_client=%s)",
        settings.llm_api_base_url,
        settings.default_llm_model,
        settings.llm_structured_output_method,
        settings.llm_auth_http_client_enabled,
    )
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=256, keepalive_expiry=30.0)
    http_async_client = (
        AuthHTTPClient(timeout=settings.llm_timeout_seconds, limits=limits)
        if settings.llm_auth_http_client_enabled
        else httpx.AsyncClient(timeout=settings.llm_timeout_seconds, limits=limits)
    )
    return LangChainLLMClient(
        base_url=settings.llm_api_base_url or "",
        api_key=settings.llm_api_key,
        default_model=settings.default_llm_model,
        temperature=getattr(settings, "llm_temperature", 0.0),
        timeout_seconds=settings.llm_timeout_seconds,
        rate_limit_max_retries=getattr(settings, "llm_rate_limit_max_retries", 2),
        rate_limit_retry_delay_seconds=getattr(settings, "llm_rate_limit_retry_delay_seconds", 2.0),
        extra_headers=settings.llm_headers,
        structured_output_method=settings.llm_structured_output_method,
        http_async_client=http_async_client,
    )


def get_router_runtime(request: Request) -> RouterRuntime:
    """Resolve or lazily create the router runtime stored on the FastAPI app state."""
    runtime = getattr(request.app.state, "router_runtime", None)
    if runtime is None:
        runtime = build_router_runtime()
        request.app.state.router_runtime = runtime
    return runtime


def get_event_broker(request: Request) -> EventBroker:
    """FastAPI dependency returning the shared SSE event broker."""
    return get_router_runtime(request).event_broker


def get_llm_client(request: Request) -> JsonLLMClient | None:
    """FastAPI dependency returning the shared LLM client when configured."""
    return get_router_runtime(request).llm_client


def get_intent_catalog(request: Request) -> RepositoryIntentCatalog:
    """FastAPI dependency returning the refreshed intent catalog view."""
    return get_router_runtime(request).intent_catalog


def get_orchestrator(request: Request) -> GraphRouterOrchestrator:
    """FastAPI dependency returning the graph router orchestrator."""
    return get_router_runtime(request).orchestrator


def get_skill_runtime(request: Request) -> SkillRuntimeController:
    """FastAPI dependency returning the markdown-first v4 Skill runtime."""
    return get_router_runtime(request).skill_runtime


def get_event_broker_v2(request: Request) -> EventBroker:
    """Compatibility alias for the V2 event broker dependency."""
    return get_event_broker(request)


def get_orchestrator_v2(request: Request) -> GraphRouterOrchestrator:
    """Compatibility alias for the V2 orchestrator dependency."""
    return get_orchestrator(request)


async def close_router_runtime(runtime: RouterRuntime) -> None:
    """Release network resources held by the runtime before application shutdown."""
    if runtime.llm_client is not None:
        await runtime.llm_client.aclose()
    await runtime.agent_client.close()


async def run_intent_catalog_refresh(
    stop_event: asyncio.Event,
    *,
    catalog: RepositoryIntentCatalog,
    refresh_interval_seconds: float,
) -> None:
    """Continuously refresh the active intent catalog until shutdown is requested."""
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


async def run_session_cleanup(
    stop_event: asyncio.Event,
    *,
    session_store: GraphSessionStore,
    cleanup_interval_seconds: float,
) -> None:
    """Periodically purge expired sessions until the application shuts down."""
    consecutive_failures = 0
    while not stop_event.is_set():
        try:
            expired_sessions = await asyncio.to_thread(session_store.purge_expired)
            if expired_sessions:
                logger.debug("purged %s expired sessions", len(expired_sessions))
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            logger.warning("session cleanup failed: %s", exc)
            if consecutive_failures >= 3:
                logger.error(
                    "session cleanup has failed %s consecutive times",
                    consecutive_failures,
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cleanup_interval_seconds)
        except asyncio.TimeoutError:
            continue
