from __future__ import annotations

import asyncio
from types import SimpleNamespace

from router_service.core.support.jwt_utils import AuthHTTPClient  # noqa: E402
from router_service.catalog.in_memory_intent_repository import InMemoryIntentRepository  # noqa: E402
from router_service.api import dependencies  # noqa: E402
from router_service.core.recognition.hierarchical_intent_recognizer import HierarchicalIntentRecognizer  # noqa: E402


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
        router_v2_understanding_mode="flat",
        router_v2_planning_policy="auto",
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
        assert runtime.orchestrator.recognizer is not None
    finally:
        asyncio.run(runtime.agent_client.close())


def test_build_llm_client_uses_plain_httpx_client_when_auth_switch_is_off(monkeypatch) -> None:
    settings = SimpleNamespace(
        llm_connection_ready=True,
        default_llm_model="test-model",
        llm_api_base_url="https://example.com/v1",
        llm_api_key="env-api-key",
        llm_auth_http_client_enabled=False,
        llm_timeout_seconds=5.0,
        llm_rate_limit_max_retries=2,
        llm_rate_limit_retry_delay_seconds=2.0,
        llm_headers={},
        llm_structured_output_method="json_mode",
    )
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)

    client = dependencies._build_llm_client()
    assert client is not None
    try:
        assert isinstance(client.http_async_client, dependencies.httpx.AsyncClient)
        assert not isinstance(client.http_async_client, AuthHTTPClient)
        assert client.api_key == "env-api-key"
    finally:
        asyncio.run(client.aclose())


def test_build_llm_client_uses_auth_http_client_when_auth_switch_is_on(monkeypatch) -> None:
    settings = SimpleNamespace(
        llm_connection_ready=True,
        default_llm_model="test-model",
        llm_api_base_url="https://example.com/v1",
        llm_api_key="ignored-when-auth-client-enabled",
        llm_auth_http_client_enabled=True,
        llm_timeout_seconds=5.0,
        llm_rate_limit_max_retries=2,
        llm_rate_limit_retry_delay_seconds=2.0,
        llm_headers={},
        llm_structured_output_method="json_mode",
    )
    monkeypatch.setattr(dependencies, "get_settings", lambda: settings)

    client = dependencies._build_llm_client()
    assert client is not None
    try:
        assert isinstance(client.http_async_client, AuthHTTPClient)
        assert client.api_key == "ignored-when-auth-client-enabled"
    finally:
        asyncio.run(client.aclose())


def test_build_router_runtime_can_enable_unified_v2_graph_builder(monkeypatch) -> None:
    settings = SimpleNamespace(
        router_sse_heartbeat_seconds=15.0,
        router_sse_max_idle_seconds=300.0,
        router_intent_refresh_interval_seconds=5.0,
        recognizer_backend="llm",
        router_v2_graph_build_mode="unified",
        router_v2_understanding_mode="flat",
        router_v2_planning_policy="always",
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
        assert runtime.orchestrator.graph_builder is not None
        assert runtime.orchestrator.recognizer is not None
    finally:
        asyncio.run(runtime.agent_client.close())


def test_build_router_runtime_disables_unified_graph_builder_in_hierarchical_mode(monkeypatch) -> None:
    settings = SimpleNamespace(
        router_sse_heartbeat_seconds=15.0,
        router_sse_max_idle_seconds=300.0,
        router_intent_refresh_interval_seconds=5.0,
        recognizer_backend="llm",
        router_v2_graph_build_mode="unified",
        router_v2_understanding_mode="hierarchical",
        router_v2_planning_policy="always",
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
        assert runtime.orchestrator.graph_builder is None
        assert isinstance(runtime.orchestrator.recognizer, HierarchicalIntentRecognizer)
    finally:
        asyncio.run(runtime.agent_client.close())
