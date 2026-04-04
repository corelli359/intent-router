from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from router_core.agent_client import StreamingAgentClient  # noqa: E402
from router_core.domain import IntentDefinition, Task, TaskStatus  # noqa: E402
from router_core.llm_client import (  # noqa: E402
    IntentRecognitionMatchPayload,
    IntentRecognitionPayload,
)
from router_core.recognizer import LLMIntentRecognizer  # noqa: E402


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


def test_streaming_agent_client_closes_owned_http_pool() -> None:
    async def run() -> None:
        client = StreamingAgentClient()

        assert client.http_client.is_closed is False
        await client.close()
        assert client.http_client.is_closed is True

    asyncio.run(run())
