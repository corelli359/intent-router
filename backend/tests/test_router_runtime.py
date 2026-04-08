from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from persistence.in_memory_intent_repository import InMemoryIntentRepository  # noqa: E402
from router_api import dependencies  # noqa: E402


class FakeLLMClient:
    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - never called
        raise AssertionError("runtime wiring test should not invoke LLM")


def test_build_router_runtime_shares_single_recognizer_instance(monkeypatch) -> None:
    settings = SimpleNamespace(
        router_sse_heartbeat_seconds=15.0,
        router_sse_max_idle_seconds=300.0,
        router_intent_refresh_interval_seconds=5.0,
        recognizer_backend="llm",
        router_v2_graph_build_mode="legacy",
        llm_recognizer_model="recognizer-model",
        llm_model="planner-model",
        llm_recognizer_system_prompt_template=None,
        llm_recognizer_human_prompt_template=None,
        agent_http_timeout_seconds=5.0,
        router_intent_switch_threshold=0.8,
        router_agent_timeout_seconds=30.0,
    )

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(dependencies, "get_intent_repository", lambda: InMemoryIntentRepository())
    monkeypatch.setattr(dependencies, "_build_llm_client", lambda: FakeLLMClient())

    runtime = dependencies.build_router_runtime()
    try:
        assert runtime.orchestrator.recognizer is runtime.orchestrator_v2.recognizer
    finally:
        asyncio.run(runtime.agent_client.close())


def test_build_router_runtime_can_enable_unified_v2_graph_builder(monkeypatch) -> None:
    settings = SimpleNamespace(
        router_sse_heartbeat_seconds=15.0,
        router_sse_max_idle_seconds=300.0,
        router_intent_refresh_interval_seconds=5.0,
        recognizer_backend="llm",
        router_v2_graph_build_mode="unified",
        llm_recognizer_model="recognizer-model",
        llm_model="planner-model",
        llm_recognizer_system_prompt_template=None,
        llm_recognizer_human_prompt_template=None,
        agent_http_timeout_seconds=5.0,
        router_intent_switch_threshold=0.8,
        router_agent_timeout_seconds=30.0,
    )

    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)
    monkeypatch.setattr(dependencies, "get_intent_repository", lambda: InMemoryIntentRepository())
    monkeypatch.setattr(dependencies, "_build_llm_client", lambda: FakeLLMClient())

    runtime = dependencies.build_router_runtime()
    try:
        assert runtime.orchestrator_v2.graph_builder is not None
        assert runtime.orchestrator_v2.recognizer is runtime.orchestrator.recognizer
    finally:
        asyncio.run(runtime.agent_client.close())
