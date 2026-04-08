from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from fastapi import Request

from admin_api.dependencies import get_intent_repository
from admin_api.dependencies import get_settings
from router_api.sse.broker import EventBroker
from router_core.agent_client import StreamingAgentClient
from router_core.intent_catalog import RepositoryIntentCatalog
from router_core.llm_client import LangChainLLMClient
from router_core.orchestrator import RouterOrchestrator, RouterOrchestratorConfig
from router_core.prompt_templates import DEFAULT_RECOGNIZER_HUMAN_PROMPT, DEFAULT_RECOGNIZER_SYSTEM_PROMPT
from router_core.recognizer import LLMIntentRecognizer, NullIntentRecognizer
from router_core.v2_orchestrator import GraphRouterOrchestrator, GraphRouterOrchestratorConfig
from router_core.v2_planner import (
    BasicTurnInterpreter,
    LLMGraphTurnInterpreter,
    LLMIntentGraphPlanner,
    SequentialIntentGraphPlanner,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RouterRuntime:
    event_broker: EventBroker
    event_broker_v2: EventBroker
    llm_client: LangChainLLMClient | None
    intent_catalog: RepositoryIntentCatalog
    agent_client: StreamingAgentClient
    orchestrator: RouterOrchestrator
    orchestrator_v2: GraphRouterOrchestrator


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
    event_broker_v2 = EventBroker(
        heartbeat_interval_seconds=settings.router_sse_heartbeat_seconds,
        max_idle_seconds=settings.router_sse_max_idle_seconds,
    )
    llm_client = _build_llm_client()
    intent_catalog = RepositoryIntentCatalog(get_intent_repository())
    recognizer = (
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
    agent_client = StreamingAgentClient(http_timeout_seconds=settings.agent_http_timeout_seconds)
    orchestrator = RouterOrchestrator(
        publish_event=event_broker.publish,
        intent_catalog=intent_catalog,
        recognizer=recognizer,
        agent_client=agent_client,
        config=RouterOrchestratorConfig(
            intent_switch_threshold=settings.router_intent_switch_threshold,
            agent_timeout_seconds=settings.router_agent_timeout_seconds,
        ),
    )
    orchestrator_v2 = GraphRouterOrchestrator(
        publish_event=event_broker_v2.publish,
        intent_catalog=intent_catalog,
        recognizer=recognizer,
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
        agent_client=agent_client,
        config=GraphRouterOrchestratorConfig(
            intent_switch_threshold=settings.router_intent_switch_threshold,
            agent_timeout_seconds=settings.router_agent_timeout_seconds,
        ),
    )
    return RouterRuntime(
        event_broker=event_broker,
        event_broker_v2=event_broker_v2,
        llm_client=llm_client,
        intent_catalog=intent_catalog,
        agent_client=agent_client,
        orchestrator=orchestrator,
        orchestrator_v2=orchestrator_v2,
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


def get_orchestrator(request: Request) -> RouterOrchestrator:
    return get_router_runtime(request).orchestrator


def get_event_broker_v2(request: Request) -> EventBroker:
    return get_router_runtime(request).event_broker_v2


def get_orchestrator_v2(request: Request) -> GraphRouterOrchestrator:
    return get_router_runtime(request).orchestrator_v2


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
