from __future__ import annotations

import asyncio
import httpx
import json
import sys
from pathlib import Path
from typing import Any

import pytest


from router_service.core.support.agent_client import StreamingAgentClient  # noqa: E402
from router_service.core.shared.domain import IntentDefinition, Task, TaskStatus  # noqa: E402
from router_service.core.shared.domain import IntentMatch  # noqa: E402
from router_service.core.support.llm_client import (  # noqa: E402
    IntentRecognitionMatchPayload,
    IntentRecognitionPayload,
    LangChainLLMClient,
)
from router_service.core.support.jwt_utils import AuthHTTPClient  # noqa: E402
from router_service.core.recognition.recognizer import LLMIntentRecognizer, NullIntentRecognizer, RecognitionResult  # noqa: E402
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState  # noqa: E402
from router_service.core.graph.planner import (  # noqa: E402
    LLMGraphTurnInterpreter,
    LLMIntentGraphPlanner,
)
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402


class FakeLangChainClient:
    def __init__(self, *, recognition_response: IntentRecognitionPayload | None = None) -> None:
        self.recognition_response = recognition_response or IntentRecognitionPayload()
        self.last_recognition_call: dict[str, Any] | None = None

    async def run_json(
        self,
        *,
        prompt,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta=None,
    ) -> Any:
        self.last_recognition_call = {
            "prompt": prompt,
            "variables": variables,
            "model": model,
        }
        if on_delta is not None:
            await on_delta('{"matches":')
        return self.recognition_response.model_dump()


def test_llm_intent_recognizer_uses_registered_intent_catalog_payload() -> None:
    async def run() -> None:
        intents = [
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账",
                examples=["给张三转 200 元"],
                keywords=["转账"],
                agent_url="https://agent.example.com/transfer",
                dispatch_priority=100,
                primary_threshold=0.7,
                candidate_threshold=0.5,
            ),
            IntentDefinition(
                intent_code="pay_bill",
                name="缴费",
                description="处理生活缴费",
                examples=["交电费"],
                keywords=["缴费"],
                agent_url="https://agent.example.com/bill",
                dispatch_priority=90,
                primary_threshold=0.8,
                candidate_threshold=0.6,
            ),
        ]
        llm_client = FakeLangChainClient(
            recognition_response=IntentRecognitionPayload(
                matches=[
                    IntentRecognitionMatchPayload(
                        intent_code="transfer_money",
                        confidence=0.91,
                        reason="用户明确提出转账",
                    ),
                    IntentRecognitionMatchPayload(
                        intent_code="pay_bill",
                        confidence=0.63,
                        reason="用户顺带提到电费",
                    ),
                ]
            )
        )

        recognizer = LLMIntentRecognizer(llm_client, model="recognizer-model")
        result = await recognizer.recognize(
            message="帮我给张三转 200 元，顺便交一下电费",
            intents=intents,
            recent_messages=["user: 你好"],
            long_term_memory=["常用收款人：张三"],
        )

        assert llm_client.last_recognition_call is not None
        assert llm_client.last_recognition_call["model"] == "recognizer-model"
        first_intent_payload = json.loads(llm_client.last_recognition_call["variables"]["intents_json"])[0]
        assert "agent_url" not in first_intent_payload
        assert "request_schema" not in first_intent_payload
        assert "field_mapping" not in first_intent_payload
        assert [match.intent_code for match in result.primary] == ["transfer_money"]
        assert [match.intent_code for match in result.candidates] == ["pay_bill"]

    asyncio.run(run())


def test_llm_intent_recognizer_can_fail_closed_without_regex_fallback() -> None:
    class FailingClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            raise RuntimeError("llm unavailable")

    async def run() -> None:
        intents = [
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账",
                examples=["给张三转 200 元"],
                keywords=["转账", "汇款"],
                agent_url="https://agent.example.com/transfer",
                dispatch_priority=100,
                primary_threshold=0.7,
                candidate_threshold=0.5,
            )
        ]

        recognizer = LLMIntentRecognizer(FailingClient())
        result = await recognizer.recognize(
            message="帮我转账给张三 200 元",
            intents=intents,
            recent_messages=[],
            long_term_memory=[],
        )

        assert result.primary == []
        assert result.candidates == []

    asyncio.run(run())


def test_llm_intent_recognizer_propagates_retryable_llm_errors() -> None:
    class _FakeRateLimitError(Exception):
        def __init__(self) -> None:
            super().__init__("rate limited")
            self.status_code = 429

    class RateLimitedClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, variables, model, on_delta
            raise _FakeRateLimitError()

    async def run() -> None:
        intents = [
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账",
                examples=["给张三转 200 元"],
                keywords=["转账", "汇款"],
                agent_url="https://agent.example.com/transfer",
                dispatch_priority=100,
                primary_threshold=0.7,
                candidate_threshold=0.5,
            )
        ]

        recognizer = LLMIntentRecognizer(RateLimitedClient())
        with pytest.raises(_FakeRateLimitError):
            await recognizer.recognize(
                message="帮我转账给张三 200 元",
                intents=intents,
                recent_messages=[],
                long_term_memory=[],
            )

    asyncio.run(run())


def test_langchain_llm_client_retries_rate_limited_requests_once() -> None:
    class _FakeRateLimitError(Exception):
        def __init__(self) -> None:
            super().__init__("rate limited")
            self.status_code = 429
            self.body = {"error": {"type": "rate_limit_error"}}

    class _RetryableClient(LangChainLLMClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="https://example.com",
                default_model="test-model",
                rate_limit_max_retries=1,
                rate_limit_retry_delay_seconds=0,
            )
            self.calls = 0

        async def _stream_once(self, prompt, variables, *, model=None, on_delta=None) -> str:
            del prompt, variables, model, on_delta
            self.calls += 1
            if self.calls == 1:
                raise _FakeRateLimitError()
            return '{"matches":[]}'

    async def run() -> None:
        client = _RetryableClient()
        payload = await client.run_json(
            prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
            variables={},
        )

        assert payload == {"matches": []}
        assert client.calls == 2

    asyncio.run(run())


def test_langchain_llm_client_passes_custom_http_async_client_to_chat_openai(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("router_service.core.support.llm_client.ChatOpenAI", _FakeChatOpenAI)
    custom_client = AuthHTTPClient()
    client = LangChainLLMClient(
        base_url="https://example.com/v1",
        api_key=None,
        default_model="test-model",
        http_async_client=custom_client,
    )

    client._create_model()

    assert captured["http_async_client"] is custom_client
    assert captured["openai_api_key"] == "jwt-auth-placeholder"


def test_langchain_llm_client_uses_empty_api_key_with_plain_httpx_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("router_service.core.support.llm_client.ChatOpenAI", _FakeChatOpenAI)
    plain_client = httpx.AsyncClient()
    client = LangChainLLMClient(
        base_url="https://example.com/v1",
        api_key=None,
        default_model="test-model",
        http_async_client=plain_client,
    )

    try:
        client._create_model()
    finally:
        asyncio.run(plain_client.aclose())

    assert captured["http_async_client"] is plain_client
    assert captured["openai_api_key"] == ""


def test_streaming_agent_client_supports_http_agent_payload_mapping() -> None:
    import httpx

    async def run() -> None:
        captured_payload: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_payload.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "event": "final",
                    "content": "订单 123 当前状态为已发货",
                    "ishandover": True,
                    "status": "completed",
                    "payload": {"order_id": "123"},
                },
            )

        task = Task(
            session_id="session_002",
            intent_code="query_order_status",
            agent_url="https://agent.example.com/order/stream",
            intent_name="查询订单状态",
            intent_description="查询订单和物流状态",
            confidence=0.88,
            request_schema={"type": "object", "required": ["sessionId", "input", "slots"]},
            field_mapping={
                "sessionId": "$session.id",
                "input": "$message.current",
                "slots.orderId": "$entities.order_id",
            },
            input_context={"recent_messages": ["user: 帮我查订单"], "long_term_memory": []},
            slot_memory={"order_id": "123"},
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "帮我查订单 123")]

        assert captured_payload["sessionId"] == "session_002"
        assert captured_payload["input"] == "帮我查订单 123"
        assert captured_payload["slots"] == {"orderId": "123"}
        assert len(chunks) == 1
        assert chunks[0].status == TaskStatus.COMPLETED
        assert chunks[0].payload["order_id"] == "123"

    asyncio.run(run())


def test_streaming_agent_client_posts_cancel_requests_to_agent_endpoint() -> None:
    import httpx

    async def run() -> None:
        captured_requests: list[tuple[str, dict[str, object]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(
                (str(request.url), json.loads(request.content.decode("utf-8")))
            )
            return httpx.Response(200, json={"status": "cancelled", "accepted": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            await client.cancel(
                session_id="session_123",
                task_id="task_123",
                agent_url="https://agent.example.com/api/agent/run",
            )

        assert captured_requests == [
            (
                "https://agent.example.com/api/agent/cancel",
                {"sessionId": "session_123", "taskId": "task_123"},
            )
        ]

    asyncio.run(run())


def test_streaming_agent_client_rejects_mock_scheme_in_stream() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_unsupported",
            intent_code="query_account_balance",
            agent_url="mock://query_account_balance",
            intent_name="查询账户余额",
            confidence=0.88,
        )

        client = StreamingAgentClient()
        try:
            chunks = [chunk async for chunk in client.stream(task, "帮我查余额")]
        finally:
            await client.close()

        assert len(chunks) == 1
        assert chunks[0].status == TaskStatus.FAILED
        assert "Unsupported agent_url scheme" in chunks[0].content

    asyncio.run(run())


def test_streaming_agent_client_rejects_mock_scheme_in_cancel() -> None:
    async def run() -> None:
        client = StreamingAgentClient()
        try:
            try:
                await client.cancel(
                    session_id="session_unsupported",
                    task_id="task_unsupported",
                    agent_url="mock://query_account_balance",
                )
            except RuntimeError as exc:
                assert "Unsupported agent_url scheme" in str(exc)
            else:
                raise AssertionError("cancel() should reject mock schemes")
        finally:
            await client.close()

    asyncio.run(run())


def test_streaming_agent_client_closes_owned_http_pool() -> None:
    async def run() -> None:
        client = StreamingAgentClient()

        assert client.http_client.is_closed is False
        await client.close()
        assert client.http_client.is_closed is True

    asyncio.run(run())


def test_llm_graph_planner_converts_structured_payload_to_execution_graph() -> None:
    class FakePlannerClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            return {
                "summary": "识别到 2 个事项，先查余额，再转账",
                "needs_confirmation": True,
                "nodes": [
                    {
                        "intent_code": "query_account_balance",
                        "title": "查询账户余额",
                        "confidence": 0.97,
                        "source_fragment": "先查余额",
                    },
                    {
                        "intent_code": "transfer_money",
                        "title": "给张三转账 200 元",
                        "confidence": 0.93,
                        "source_fragment": "再给张三转账 200 元",
                        "slot_memory": {"recipient_name": "张三", "amount": "200"},
                    },
                ],
                "edges": [
                    {
                        "source_index": 0,
                        "target_index": 1,
                        "relation_type": "sequential",
                        "label": "先查询再执行转账",
                    }
                ],
            }

    async def run() -> None:
        planner = LLMIntentGraphPlanner(FakePlannerClient(), model="graph-planner-model")
        intents = {
            "query_account_balance": IntentDefinition(
                intent_code="query_account_balance",
                name="查询账户余额",
                description="查询账户余额",
                examples=["查余额"],
                agent_url="https://agent.example.com/balance",
            ),
            "transfer_money": IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账",
                examples=["给张三转账"],
                agent_url="https://agent.example.com/transfer",
            ),
        }
        graph = await planner.plan(
            message="先查余额，再给张三转账 200 元",
            matches=[
                IntentMatch(intent_code="query_account_balance", confidence=0.97, reason="fixed"),
                IntentMatch(intent_code="transfer_money", confidence=0.93, reason="fixed"),
            ],
            intents_by_code=intents,
            recent_messages=[],
            long_term_memory=[],
        )

        assert graph.summary == "识别到 2 个事项，先查余额，再转账"
        assert [node.intent_code for node in graph.nodes] == ["query_account_balance", "transfer_money"]
        assert graph.nodes[1].slot_memory == {"recipient_name": "张三", "amount": "200"}
        assert graph.edges[0].relation_type.value == "sequential"
        assert graph.edges[0].label == "先查询再执行转账"

    asyncio.run(run())


def test_llm_turn_interpreter_uses_structured_llm_decision() -> None:
    class FakeTurnClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            return {
                "action": "replan",
                "reason": "用户明确切换为新的余额查询诉求",
                "target_intent_code": "query_account_balance",
            }

    async def run() -> None:
        interpreter = LLMGraphTurnInterpreter(FakeTurnClient(), model="turn-model")
        decision = await interpreter.interpret_waiting_node(
            message="别转了，先帮我查余额",
            waiting_node=GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.9,
            ),
            current_graph=ExecutionGraphState(source_message="帮我转账"),
            recognition=RecognitionResult(
                primary=[
                    IntentMatch(
                        intent_code="query_account_balance",
                        confidence=0.98,
                        reason="explicit switch",
                    )
                ],
                candidates=[],
            ),
        )

        assert decision.action == "replan"
        assert decision.target_intent_code == "query_account_balance"

    asyncio.run(run())
