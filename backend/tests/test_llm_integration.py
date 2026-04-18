from __future__ import annotations

import asyncio
import httpx
import json
import logging
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
    extract_json_value,
)
from router_service.core.support.jwt_utils import AuthHTTPClient  # noqa: E402
from router_service.core.recognition.recognizer import (  # noqa: E402
    LLMIntentRecognizer,
    NullIntentRecognizer,
    RecognitionResult,
    recognition_intents_json,
)
from router_service.core.shared.graph_domain import (  # noqa: E402
    ExecutionGraphState,
    GraphNodeState,
    ProactiveRecommendationItem,
    ProactiveRecommendationPayload,
    ProactiveRecommendationRouteMode,
)
from router_service.core.graph.planner import (  # noqa: E402
    LLMGraphTurnInterpreter,
    LLMIntentGraphPlanner,
)
from router_service.core.graph.recommendation_router import LLMProactiveRecommendationRouter  # noqa: E402
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
        assert result.diagnostics
        assert result.diagnostics[0].code == "RECOGNIZER_LLM_FAILED"

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

        async def _invoke_once(self, prompt, variables, *, model=None) -> str:
            del prompt, variables, model
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


def test_langchain_llm_client_logs_elapsed_time_for_completed_call(caplog) -> None:
    class _LoggingClient(LangChainLLMClient):
        async def _stream_prompt(self, prompt, variables, *, model=None, on_delta=None) -> str:
            del prompt, variables, model, on_delta
            return '{"matches":[]}'

    async def run() -> None:
        client = _LoggingClient(
            base_url="https://example.com",
            default_model="test-model",
        )
        client_logger = logging.getLogger("router_service.core.support.llm_client")
        original_handlers = list(client_logger.handlers)
        original_level = client_logger.level
        original_propagate = client_logger.propagate
        client_logger.handlers = [caplog.handler]
        client_logger.setLevel(logging.DEBUG)
        client_logger.propagate = False
        try:
            payload = await client.run_json(
                prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                variables={"message": "hi"},
            )
        finally:
            client_logger.handlers = original_handlers
            client_logger.setLevel(original_level)
            client_logger.propagate = original_propagate

        assert payload == {"matches": []}

    asyncio.run(run())

    assert "LLM call completed" in caplog.text
    assert "LLM call request" in caplog.text
    assert "LLM call response" in caplog.text
    assert "model=test-model" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "variable_keys=message" in caplog.text
    assert '"content":"hi"' in caplog.text or '"content": "hi"' in caplog.text
    assert 'response_text={"matches":[]}' in caplog.text


def test_langchain_llm_client_logs_elapsed_time_for_failed_call(caplog) -> None:
    class _InvalidJsonClient(LangChainLLMClient):
        async def _stream_prompt(self, prompt, variables, *, model=None, on_delta=None) -> str:
            del prompt, variables, model, on_delta
            return "not-json"

    async def run() -> None:
        client = _InvalidJsonClient(
            base_url="https://example.com",
            default_model="test-model",
        )
        client_logger = logging.getLogger("router_service.core.support.llm_client")
        original_handlers = list(client_logger.handlers)
        original_level = client_logger.level
        original_propagate = client_logger.propagate
        client_logger.handlers = [caplog.handler]
        client_logger.setLevel(logging.DEBUG)
        client_logger.propagate = False
        try:
            with pytest.raises(ValueError):
                await client.run_json(
                    prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                    variables={"message": "hi"},
                )
        finally:
            client_logger.handlers = original_handlers
            client_logger.setLevel(original_level)
            client_logger.propagate = original_propagate

    asyncio.run(run())

    assert "LLM call failed" in caplog.text
    assert "LLM call request" in caplog.text
    assert "model=test-model" in caplog.text
    assert "elapsed_ms=" in caplog.text


def test_extract_json_value_parses_first_json_fragment_from_text() -> None:
    payload = extract_json_value('prefix {"matches":[{"intent_code":"transfer_money","confidence":0.9}]} suffix')
    assert payload == {"matches": [{"intent_code": "transfer_money", "confidence": 0.9}]}


def test_langchain_llm_client_uses_non_streaming_path_without_on_delta() -> None:
    class _DualPathClient(LangChainLLMClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="https://example.com",
                default_model="test-model",
            )
            self.stream_calls = 0
            self.invoke_calls = 0

        async def _stream_once(self, prompt, variables, *, model=None, on_delta=None) -> str:
            del prompt, variables, model, on_delta
            self.stream_calls += 1
            return '{"matches":[]}'

        async def _invoke_once(self, prompt, variables, *, model=None) -> str:
            del prompt, variables, model
            self.invoke_calls += 1
            return '{"matches":[]}'

    async def run() -> None:
        client = _DualPathClient()
        payload = await client.run_json(
            prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
            variables={},
        )

        assert payload == {"matches": []}
        assert client.invoke_calls == 1
        assert client.stream_calls == 0

    asyncio.run(run())


def test_langchain_llm_client_keeps_streaming_path_when_on_delta_is_provided() -> None:
    class _DualPathClient(LangChainLLMClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="https://example.com",
                default_model="test-model",
            )
            self.stream_calls = 0
            self.invoke_calls = 0

        async def _stream_once(self, prompt, variables, *, model=None, on_delta=None) -> str:
            del prompt, variables, model
            self.stream_calls += 1
            if on_delta is not None:
                await on_delta('{"matches":')
            return '{"matches":[]}'

        async def _invoke_once(self, prompt, variables, *, model=None) -> str:
            del prompt, variables, model
            self.invoke_calls += 1
            return '{"matches":[]}'

    async def run() -> None:
        deltas: list[str] = []
        client = _DualPathClient()

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        payload = await client.run_json(
            prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
            variables={},
            on_delta=on_delta,
        )

        assert payload == {"matches": []}
        assert client.stream_calls == 1
        assert client.invoke_calls == 0
        assert deltas == ['{"matches":']

    asyncio.run(run())


def test_langchain_llm_client_uses_bearer_auth_with_plain_httpx_client() -> None:
    async def run() -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            captured["content_type"] = request.headers.get("content-type")
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": '{"matches":[]}',
                            }
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as plain_client:
            client = LangChainLLMClient(
                base_url="https://example.com/v1",
                api_key="plain-api-key",
                default_model="test-model",
                http_async_client=plain_client,
            )
            payload = await client.run_json(
                prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                variables={},
            )

        assert payload == {"matches": []}
        assert captured["url"] == "https://example.com/v1/chat/completions"
        assert captured["authorization"] == "Bearer plain-api-key"
        assert captured["content_type"] == "application/json"
        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["stream"] is False

    asyncio.run(run())


def test_langchain_llm_client_uses_auth_http_client_for_per_request_jwt_headers() -> None:
    async def run() -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("Authorization")
            captured["x_app_id"] = request.headers.get("x-app-id")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": '{"matches":[]}',
                            }
                        }
                    ]
                },
            )

        async with AuthHTTPClient(transport=httpx.MockTransport(handler)) as auth_client:
            client = LangChainLLMClient(
                base_url="https://example.com/v1",
                api_key=None,
                default_model="test-model",
                http_async_client=auth_client,
            )
            payload = await client.run_json(
                prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                variables={},
            )

        assert payload == {"matches": []}
        assert isinstance(captured["authorization"], str)
        assert captured["authorization"]
        assert captured["x_app_id"] == "app-test"


def test_langchain_llm_client_parses_streaming_chunks_from_openai_compatible_sse() -> None:
    class _AsyncByteStream(httpx.AsyncByteStream):
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def __aiter__(self):
            for chunk in self._chunks:
                yield chunk

        async def aclose(self) -> None:
            return None

    async def run() -> None:
        captured_body: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                stream=_AsyncByteStream(
                    [
                        b'data: {"choices":[{"delta":{"content":"{\\"matches\\":"}}]}\n\n',
                        b'data: {"choices":[{"delta":{"content":"[]}"}}]}\n\n',
                        b"data: [DONE]\n\n",
                    ]
                ),
            )

        deltas: list[str] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as plain_client:
            client = LangChainLLMClient(
                base_url="https://example.com/v1",
                default_model="test-model",
                http_async_client=plain_client,
            )

            async def on_delta(delta: str) -> None:
                deltas.append(delta)

            payload = await client.run_json(
                prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                variables={},
                on_delta=on_delta,
            )

        assert payload == {"matches": []}
        assert captured_body["stream"] is True
        assert deltas == ['{"matches":', '[]}']


def test_langchain_llm_client_skips_prompt_render_logging_when_debug_is_disabled() -> None:
    class _LoggingClient(LangChainLLMClient):
        def __init__(self) -> None:
            super().__init__(
                base_url="https://example.com",
                default_model="test-model",
            )
            self.render_calls = 0

        def _render_prompt_messages(self, prompt, variables) -> str:
            del prompt, variables
            self.render_calls += 1
            return "[]"

        async def _stream_prompt(self, prompt, variables, *, model=None, on_delta=None) -> str:
            del prompt, variables, model, on_delta
            return '{"matches":[]}'

    async def run() -> None:
        client = _LoggingClient()
        client_logger = logging.getLogger("router_service.core.support.llm_client")
        original_level = client_logger.level
        try:
            client_logger.setLevel(logging.INFO)
            payload = await client.run_json(
                prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                variables={"message": "hi"},
            )
        finally:
            client_logger.setLevel(original_level)

        assert payload == {"matches": []}
        assert client.render_calls == 0

    asyncio.run(run())


def test_recognition_intents_json_reuses_cached_serialization_for_same_intent_instances() -> None:
    intents = [
        IntentDefinition(
            intent_code="transfer_money",
            name="转账",
            description="执行转账",
            examples=["给张三转 200 元"],
            keywords=["转账"],
            agent_url="https://agent.example.com/transfer",
        )
    ]

    first = recognition_intents_json(intents)
    second = recognition_intents_json(intents)

    assert first is second


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


def test_llm_graph_planner_falls_back_when_llm_call_fails() -> None:
    class FailingPlannerClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, variables, model, on_delta
            raise RuntimeError("planner unavailable")

    async def run() -> None:
        planner = LLMIntentGraphPlanner(FailingPlannerClient(), model="graph-planner-model")
        intents = {
            "query_account_balance": IntentDefinition(
                intent_code="query_account_balance",
                name="查询账户余额",
                description="查询账户余额",
                examples=["查余额"],
                agent_url="https://agent.example.com/balance",
            ),
        }
        graph = await planner.plan(
            message="帮我查一下余额",
            matches=[IntentMatch(intent_code="query_account_balance", confidence=0.97, reason="fixed")],
            intents_by_code=intents,
            recent_messages=[],
            long_term_memory=[],
        )

        assert [node.intent_code for node in graph.nodes] == ["query_account_balance"]
        assert any(item.code == "GRAPH_PLANNER_LLM_FAILED_FALLBACK" for item in graph.diagnostics or [])
        assert any(item.details.get("error_type") == "RuntimeError" for item in graph.diagnostics or [])

    asyncio.run(run())


def test_llm_turn_interpreter_falls_back_when_llm_call_fails() -> None:
    class FailingTurnClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, variables, model, on_delta
            raise RuntimeError("turn interpreter unavailable")

    async def run() -> None:
        interpreter = LLMGraphTurnInterpreter(FailingTurnClient(), model="turn-model")
        decision = await interpreter.interpret_waiting_node(
            message="继续",
            waiting_node=GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.9,
            ),
            current_graph=ExecutionGraphState(source_message="帮我转账"),
            recognition=RecognitionResult(primary=[], candidates=[]),
        )

        assert decision.action == "resume_current"

    asyncio.run(run())


def test_llm_proactive_recommendation_router_falls_back_when_llm_call_fails() -> None:
    class FailingRecommendationClient:
        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, variables, model, on_delta
            raise RuntimeError("recommendation unavailable")

    async def run() -> None:
        router = LLMProactiveRecommendationRouter(FailingRecommendationClient(), model="recommendation-model")
        decision = await router.decide(
            message="先不处理这些推荐",
            proactive_recommendation=ProactiveRecommendationPayload(
                introText="这里有推荐事项",
                items=[
                    ProactiveRecommendationItem(
                        recommendationItemId="rec-transfer",
                        intentCode="transfer_money",
                        title="给妈妈转账",
                    )
                ],
            ),
        )

        assert decision.route_mode == ProactiveRecommendationRouteMode.SWITCH_TO_FREE_DIALOG

    asyncio.run(run())
