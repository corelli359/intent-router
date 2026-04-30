from __future__ import annotations

import asyncio
import httpx
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest


from router_service.core.support.agent_client import RequestPayloadBuilder, StreamingAgentClient  # noqa: E402
from router_service.core.shared.domain import IntentDefinition, Task, TaskStatus  # noqa: E402
from router_service.core.shared.domain import IntentMatch  # noqa: E402
from router_service.core.support.llm_client import (  # noqa: E402
    IntentRecognitionMatchPayload,
    IntentRecognitionPayload,
    LLMHTTPStatusError,
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
    TurnDecisionPayload,
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


class _AsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def test_llm_intent_recognizer_uses_registered_intent_catalog_payload() -> None:
    async def run() -> None:
        intents = [
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账",
                examples=["给张三转 200 元"],
                keywords=["转账"],
                agent_id="AG_TRANS",
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
        recommend_task = [
            {
                "intentCode": "pay_bill",
                "title": "缴电费",
                "slotMemory": {"amount": 100},
            }
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
            recommend_task=recommend_task,
        )

        assert llm_client.last_recognition_call is not None
        assert llm_client.last_recognition_call["model"] == "recognizer-model"
        assert json.loads(llm_client.last_recognition_call["variables"]["recommend_task_json"]) == recommend_task
        first_intent_payload = json.loads(llm_client.last_recognition_call["variables"]["intents_json"])[0]
        assert first_intent_payload["agent_id"] == "AG_TRANS"
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


def test_llm_http_status_error_message_includes_body_preview() -> None:
    error = LLMHTTPStatusError(
        400,
        body={
            "error": {
                "message": "model not found",
                "type": "invalid_request_error",
            }
        },
    )

    assert "LLM HTTP request failed with status 400" in str(error)
    assert "model not found" in str(error)
    assert "invalid_request_error" in str(error)
    assert error.body["error"]["message"] == "model not found"


def test_langchain_llm_client_http_status_error_includes_provider_body() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "unknown model: bad-model",
                        "code": "model_not_found",
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as plain_client:
            client = LangChainLLMClient(
                base_url="https://example.com/v1",
                api_key="plain-api-key",
                default_model="bad-model",
                http_async_client=plain_client,
            )
            with pytest.raises(LLMHTTPStatusError) as exc_info:
                await client.run_json(
                    prompt=ChatPromptTemplate.from_messages([("human", "hi")]),
                    variables={},
                )

        assert exc_info.value.status_code == 400
        assert "unknown model: bad-model" in str(exc_info.value)
        assert "model_not_found" in str(exc_info.value)
        assert exc_info.value.body["error"]["code"] == "model_not_found"

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


def test_request_payload_builder_supports_config_variables_and_slots_data_mappings() -> None:
    task = Task(
        session_id="session_003",
        intent_code="transfer_money",
        agent_url="https://agent.example.com/transfer",
        intent_name="转账",
        intent_description="执行转账",
        intent_examples=["给小明转账 200 元"],
        agent_id="AG_TRANS",
        confidence=0.91,
        request_schema={"type": "object", "required": ["session_id", "txt", "stream", "config_variables"]},
        field_mapping={
            "agent_id": "legacy_mapping_value",
            "session_id": "$session.id",
            "txt": "$message.current",
            "stream": "true",
            "config_variables.custID": "$session.cust_id",
            "config_variables.currentDisplay": "",
            "config_variables.intent": "$intent",
            "config_variables.recent_messages": "$context.recent_messages",
            "config_variables.long_term_memory": "$context.long_term_memory",
            "config_variables.approval_required": "$slot_memory.approval_required",
            "config_variables.retry_count": "$slot_memory.retry_count",
            "config_variables.exchange_rate": "$slot_memory.exchange_rate",
            "config_variables.memo": "$slot_memory.memo",
            "config_variables.routing": "$slot_memory.routing",
            "config_variables.tags": "$slot_memory.tags",
            "config_variables.slots_data.payee_name": "$slot_memory.payee_name",
            "config_variables.slots_data.amount": "$slot_memory.amount",
        },
        input_context={
            "cust_id": "cust_003",
            "recent_messages": ["user: 给小明转账"],
            "long_term_memory": ["常用收款人: 小明"],
        },
        slot_memory={
            "payee_name": "小明",
            "amount": 200,
            "approval_required": True,
            "retry_count": 2,
            "exchange_rate": 6.8,
            "memo": None,
            "routing": {"channel": "fast"},
            "tags": ["vip", "mobile"],
        },
    )

    payload = RequestPayloadBuilder().build(task, "给小明转账 200 元")

    assert payload["session_id"] == "session_003"
    assert payload["txt"] == "给小明转账 200 元"
    assert payload["stream"] is True
    assert payload["agent_id"] == "AG_TRANS"
    assert "intent" not in payload

    config_variables = {item["name"]: item["value"] for item in payload["config_variables"]}
    assert config_variables["custID"] == "cust_003"
    assert config_variables["currentDisplay"] == ""
    assert json.loads(config_variables["intent"]) == {
        "code": "transfer_money",
        "name": "转账",
        "description": "执行转账",
        "examples": ["给小明转账 200 元"],
    }
    assert json.loads(config_variables["recent_messages"]) == ["user: 给小明转账"]
    assert json.loads(config_variables["long_term_memory"]) == ["常用收款人: 小明"]
    assert config_variables["approval_required"] == "true"
    assert config_variables["retry_count"] == "2"
    assert config_variables["exchange_rate"] == "6.8"
    assert config_variables["memo"] == "null"
    assert json.loads(config_variables["routing"]) == {"channel": "fast"}
    assert json.loads(config_variables["tags"]) == ["vip", "mobile"]
    assert json.loads(config_variables["slots_data"]) == {"payee_name": "小明", "amount": 200}


def test_request_payload_builder_supports_passthrough_upstream_config_variables() -> None:
    task = Task(
        session_id="session_004",
        intent_code="AG_TRANS",
        agent_url="https://agent.example.com/transfer",
        intent_name="转账",
        intent_description="执行转账",
        intent_examples=["给小明转账 200 元"],
        confidence=0.93,
        request_schema={"type": "object", "required": ["session_id", "txt", "stream", "config_variables"]},
        field_mapping={
            "session_id": "$session.id",
            "txt": "$message.current",
            "stream": "true",
            "config_variables.custID": "$config_variables.custID",
            "config_variables.sessionID": "$config_variables.sessionID",
            "config_variables.currentDisplay": "$config_variables.currentDisplay",
            "config_variables.agentSessionID": "$config_variables.agentSessionID",
            "config_variables.slots_data.payee_name": "$slot_memory.payee_name",
            "config_variables.slots_data.amount": "$slot_memory.amount",
        },
        input_context={
            "cust_id": "cust_from_session",
            "config_variables": {
                "custID": "cust_from_upstream",
                "sessionID": "session_from_upstream",
                "currentDisplay": "display_001",
                "agentSessionID": "agent_session_001",
            },
            "recent_messages": ["user: 给小明转账"],
            "long_term_memory": [],
        },
        slot_memory={
            "payee_name": "小明",
            "amount": "200",
        },
    )

    payload = RequestPayloadBuilder().build(task, "给小明转账 200 元")

    assert payload["session_id"] == "session_004"
    assert payload["txt"] == "给小明转账 200 元"
    assert payload["stream"] is True
    config_variables = {item["name"]: item["value"] for item in payload["config_variables"]}
    assert config_variables["custID"] == "cust_from_upstream"
    assert config_variables["sessionID"] == "session_from_upstream"
    assert config_variables["currentDisplay"] == "display_001"
    assert config_variables["agentSessionID"] == "agent_session_001"
    assert json.loads(config_variables["slots_data"]) == {"payee_name": "小明", "amount": "200"}


def test_request_payload_builder_keeps_legacy_default_payload_for_unmapped_intents() -> None:
    task = Task(
        session_id="session_legacy",
        intent_code="query_order_status",
        agent_url="https://agent.example.com/order/stream",
        intent_name="查询订单状态",
        intent_description="查询订单和物流状态",
        intent_examples=["帮我查订单 123"],
        agent_id="AG_ORDER",
        confidence=0.88,
        input_context={"recent_messages": ["user: 帮我查订单"], "long_term_memory": ["订单 123 常查"]},
        slot_memory={"order_id": "123"},
    )

    payload = RequestPayloadBuilder().build(task, "帮我查订单 123")

    assert payload == {
        "sessionId": "session_legacy",
        "taskId": task.task_id,
        "intentCode": "query_order_status",
        "agent_id": "AG_ORDER",
        "input": "帮我查订单 123",
        "intent": {
            "code": "query_order_status",
            "name": "查询订单状态",
            "description": "查询订单和物流状态",
            "examples": ["帮我查订单 123"],
        },
        "context": {
            "recentMessages": ["user: 帮我查订单"],
            "longTermMemory": ["订单 123 常查"],
        },
        "slots": {"order_id": "123"},
    }


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
                    "completion_state": 2,
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


def test_streaming_agent_client_treats_missing_completion_state_as_running() -> None:
    import httpx

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "event": "final",
                    "content": "Agent 已交接，但没有给完成信号",
                    "isHandOver": True,
                    "payload": {"trace": "missing_completion_state"},
                },
            )

        task = Task(
            session_id="session_missing_completion",
            intent_code="query_order_status",
            agent_url="https://agent.example.com/order/stream",
            intent_name="查询订单状态",
            intent_description="查询订单和物流状态",
            confidence=0.88,
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "查订单")]

        assert len(chunks) == 1
        assert chunks[0].status == TaskStatus.RUNNING
        assert chunks[0].ishandover is True
        assert "completion_state" not in chunks[0].output
        assert "status" not in chunks[0].output

    asyncio.run(run())


def test_streaming_agent_client_supports_sse_json_payloads() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_sse",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncByteStream(
                    [
                        b'event: message\ndata: {"event":"final","content":"\xe5\xb7\xb2\xe5\xae\x8c\xe6\x88\x90","ishandover":true,"node_id":"end","completion_state":2,"completion_reason":"agent_final_done","payload":{"receipt_id":"txn_001"}}\n\n',
                        b"event: done\ndata: [DONE]\n\n",
                    ]
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]

        assert len(chunks) == 1
        assert chunks[0].content == "已完成"
        assert chunks[0].status == TaskStatus.COMPLETED
        assert chunks[0].ishandover is True
        assert chunks[0].payload == {"receipt_id": "txn_001"}
        assert chunks[0].output["node_id"] == "end"
        assert chunks[0].output["completion_state"] == 2
        assert chunks[0].output["completion_reason"] == "agent_final_done"
        assert chunks[0].output["payload"] == {"receipt_id": "txn_001"}

    asyncio.run(run())


def test_streaming_agent_client_keeps_agent_message_fields_in_output() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_sse_message",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncByteStream(
                    [
                        (
                            b'event: message\ndata: {"event":"message","message":"'
                            b'\xe6\x94\xb6\xe6\xac\xbe\xe4\xba\xba\xe6\xa0\xa1\xe9\xaa\x8c\xe9\x80\x9a\xe8\xbf\x87'
                            b'","node_id":"validate_payee","completion_state":0}\n\n'
                        ),
                        b"event: done\ndata: [DONE]\n\n",
                    ]
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]

        assert len(chunks) == 1
        assert chunks[0].content == "收款人校验通过"
        assert chunks[0].output["message"] == "收款人校验通过"
        assert chunks[0].output["node_id"] == "validate_payee"
        assert chunks[0].output["completion_state"] == 0

    asyncio.run(run())


def test_streaming_agent_client_normalizes_legacy_nested_agent_payloads() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_nested",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={
                    "additional_kwargs": {
                        "node_id": "end",
                        "node_output": {
                            "output": json.dumps(
                                {
                                "event": "final",
                                "data": [{"answer": "已向小明转账 200 CNY，转账成功"}],
                                "isHandOver": True,
                                "completion_state": 2,
                                "completion_reason": "agent_final_done",
                                "slot_memory": {"payee_name": "小明", "amount": "200"},
                                "payload": {"receipt_id": "txn_123"},
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]

        assert len(chunks) == 1
        assert chunks[0].content == "已向小明转账 200 CNY，转账成功"
        assert chunks[0].status == TaskStatus.COMPLETED
        assert chunks[0].ishandover is True
        assert chunks[0].payload == {
            "receipt_id": "txn_123",
            "slot_memory": {"payee_name": "小明", "amount": "200"},
        }
        assert task.slot_memory == {"payee_name": "小明", "amount": "200"}
        assert chunks[0].output["node_id"] == "end"
        assert chunks[0].output["completion_state"] == 2
        assert chunks[0].output["completion_reason"] == "agent_final_done"
        assert chunks[0].output["payload"] == {"receipt_id": "txn_123"}
        assert "slot_memory" not in chunks[0].output

    asyncio.run(run())


def test_streaming_agent_client_normalizes_top_level_output_wrapper() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_output_wrapper",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncByteStream(
                    [
                        (
                            'event: message\ndata: {"event":"message","output":{"node_id":"validate_payee",'
                            '"message":"收款人校验通过","completion_state":0,"isHandOver":false,'
                            '"data":[{"answer":"收款人校验通过"}],'
                            '"slot_memory":{"payee_name":"小明","amount":"200"}}}\n\n'
                        ).encode("utf-8"),
                        b"event: done\ndata: [DONE]\n\n",
                    ]
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]

        assert len(chunks) == 1
        assert chunks[0].content == "收款人校验通过"
        assert chunks[0].status == TaskStatus.RUNNING
        assert chunks[0].ishandover is False
        assert chunks[0].payload == {"slot_memory": {"payee_name": "小明", "amount": "200"}}
        assert task.slot_memory == {"payee_name": "小明", "amount": "200"}
        assert chunks[0].output["node_id"] == "validate_payee"
        assert chunks[0].output["message"] == "收款人校验通过"
        assert chunks[0].output["completion_state"] == 0
        assert chunks[0].output["data"] == [{"answer": "收款人校验通过"}]
        assert "slot_memory" not in chunks[0].output

    asyncio.run(run())


def test_streaming_agent_client_reports_invalid_json_stream_payloads() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_invalid_stream",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncByteStream([b"data: not-json\n\n"]),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]

        assert len(chunks) == 1
        assert chunks[0].status == TaskStatus.FAILED
        assert "invalid JSON stream payload" in chunks[0].content

    asyncio.run(run())


def test_streaming_agent_client_logs_http_error_details(caplog) -> None:
    async def run() -> None:
        task = Task(
            session_id="session_http_error",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(502, text='{"error":"bad gateway"}')

        client_logger = logging.getLogger("router_service.core.support.agent_client")
        original_handlers = list(client_logger.handlers)
        original_level = client_logger.level
        original_propagate = client_logger.propagate
        client_logger.handlers = [caplog.handler]
        client_logger.setLevel(logging.DEBUG)
        client_logger.propagate = False
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = StreamingAgentClient(http_client=http_client)
                chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]
        finally:
            client_logger.handlers = original_handlers
            client_logger.setLevel(original_level)
            client_logger.propagate = original_propagate

        assert len(chunks) == 1
        assert chunks[0].status == TaskStatus.FAILED

    asyncio.run(run())

    assert "Agent request started" in caplog.text
    assert "Agent HTTP request failed" in caplog.text
    assert "status_code=502" in caplog.text
    assert 'body={"error":"bad gateway"}' in caplog.text


def test_streaming_agent_client_logs_parse_failures_and_empty_streams(caplog) -> None:
    async def run() -> None:
        invalid_task = Task(
            session_id="session_invalid_stream_log",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )
        empty_task = Task(
            session_id="session_empty_stream_log",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        responses = iter(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_AsyncByteStream([b"data: not-json\n\n"]),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_AsyncByteStream([b"data: [DONE]\n\n"]),
                ),
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return next(responses)

        client_logger = logging.getLogger("router_service.core.support.agent_client")
        original_handlers = list(client_logger.handlers)
        original_level = client_logger.level
        original_propagate = client_logger.propagate
        client_logger.handlers = [caplog.handler]
        client_logger.setLevel(logging.DEBUG)
        client_logger.propagate = False
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                client = StreamingAgentClient(http_client=http_client)
                invalid_chunks = [chunk async for chunk in client.stream(invalid_task, "给小明转账 200 元")]
                empty_chunks = [chunk async for chunk in client.stream(empty_task, "给小明转账 200 元")]
        finally:
            client_logger.handlers = original_handlers
            client_logger.setLevel(original_level)
            client_logger.propagate = original_propagate

        assert invalid_chunks[0].status == TaskStatus.FAILED
        assert empty_chunks[0].status == TaskStatus.FAILED

    asyncio.run(run())

    assert "Agent response parse failed" in caplog.text
    assert "invalid JSON stream payload" in caplog.text
    assert "Agent returned no stream events" in caplog.text


def test_streaming_agent_client_reports_empty_streams() -> None:
    async def run() -> None:
        task = Task(
            session_id="session_empty_stream",
            intent_code="transfer_money",
            agent_url="https://agent.example.com/transfer",
            intent_name="转账",
            confidence=0.93,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncByteStream([b"data: [DONE]\n\n"]),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = StreamingAgentClient(http_client=http_client)
            chunks = [chunk async for chunk in client.stream(task, "给小明转账 200 元")]

        assert len(chunks) == 1
        assert chunks[0].status == TaskStatus.FAILED
        assert chunks[0].content == "Agent returned no stream events"

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
        def __init__(self) -> None:
            self.variables: dict[str, Any] | None = None

        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, model, on_delta
            self.variables = dict(variables)
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
        client = FakePlannerClient()
        planner = LLMIntentGraphPlanner(client, model="graph-planner-model")
        recommend_task = [{"intentCode": "transfer_money", "title": "给张三转账"}]
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
            recommend_task=recommend_task,
        )

        assert client.variables is not None
        assert json.loads(client.variables["recommend_task_json"]) == recommend_task
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


def test_llm_blocked_turn_interpreter_merges_action_and_intent_recognition() -> None:
    class FakeTurnClient:
        def __init__(self) -> None:
            self.variables: dict[str, Any] | None = None
            self.model: str | None = None

        async def run_json(self, *, prompt, variables, model=None, on_delta=None):
            del prompt, on_delta
            self.variables = dict(variables)
            self.model = model
            return {
                "action": "replan",
                "reason": "用户在补槽阶段切换为余额查询",
                "primary_intents": [
                    {
                        "intent_code": "query_account_balance",
                        "confidence": 0.98,
                        "reason": "明确提出查余额",
                    }
                ],
                "candidate_intents": [],
            }

    async def run() -> None:
        client = FakeTurnClient()
        interpreter = LLMGraphTurnInterpreter(client, model="turn-model")
        recommend_task = [{"intentCode": "transfer_money", "title": "给张三转账"}]
        decision = await interpreter.interpret_blocked_turn(
            mode="waiting_node",
            message="别转了，先帮我查余额",
            waiting_node=GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.9,
            ),
            current_graph=ExecutionGraphState(source_message="帮我转账"),
            active_intents=[
                IntentDefinition(
                    intent_code="query_account_balance",
                    name="查询余额",
                    description="查询账户余额",
                    examples=["查余额"],
                    agent_url="https://agent.example.com/balance",
                ),
                IntentDefinition(
                    intent_code="transfer_money",
                    name="转账",
                    description="执行转账",
                    examples=["给张三转账"],
                    agent_url="https://agent.example.com/transfer",
                ),
            ],
            recent_messages=["user: 我要转账", "assistant: 请提供金额"],
            long_term_memory=["常用账户：工资卡"],
            recommend_task=recommend_task,
        )

        assert client.model == "turn-model"
        assert client.variables is not None
        assert json.loads(client.variables["recommend_task_json"]) == recommend_task
        assert json.loads(client.variables["recent_messages_json"]) == ["user: 我要转账", "assistant: 请提供金额"]
        assert json.loads(client.variables["long_term_memory_json"]) == ["常用账户：工资卡"]
        assert [item["intent_code"] for item in json.loads(client.variables["intents_json"])] == [
            "query_account_balance",
            "transfer_money",
        ]
        assert json.loads(client.variables["primary_intents_json"]) == []
        assert decision.action == "replan"
        assert decision.target_intent_code == "query_account_balance"
        assert [match.intent_code for match in decision.primary_intents] == ["query_account_balance"]

    asyncio.run(run())


def test_turn_decision_clears_intent_fields_for_non_replan_actions() -> None:
    decision = TurnDecisionPayload(
        action="cancel_current",
        reason="用户取消当前任务",
        target_intent_code="transfer_money",
        primary_intents=[
            IntentMatch(
                intent_code="query_account_balance",
                confidence=0.98,
                reason="should be ignored",
            )
        ],
        candidate_intents=[
            IntentMatch(
                intent_code="pay_gas_bill",
                confidence=0.72,
                reason="should be ignored",
            )
        ],
    )

    assert decision.target_intent_code is None
    assert decision.primary_intents == []
    assert decision.candidate_intents == []


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
