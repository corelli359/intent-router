from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx

from router_api.dependencies import get_orchestrator
from router_api.app import create_router_app
from router_api.sse.broker import EventBroker
from router_core.agent_client import MockStreamingAgentClient, StreamingAgentClient
from router_core.domain import IntentDefinition, utc_now
from router_core.orchestrator import RouterOrchestrator


class _StaticCatalog:
    def __init__(self, intents: list[IntentDefinition]) -> None:
        self._intents = intents

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def priorities(self) -> dict[str, int]:
        return {intent.intent_code: intent.dispatch_priority for intent in self._intents}


def _mock_intents() -> list[IntentDefinition]:
    return [
        IntentDefinition(
            intent_code="query_order_status",
            name="查询订单状态",
            description="查询订单状态、物流状态、订单进度。",
            examples=["帮我查下订单状态", "订单 123 到哪了"],
            keywords=["订单", "物流", "状态"],
            agent_url="mock://query_order_status",
            dispatch_priority=100,
            primary_threshold=0.68,
            candidate_threshold=0.45,
        ),
        IntentDefinition(
            intent_code="cancel_appointment",
            name="取消预约",
            description="取消预约",
            examples=["帮我取消明天的预约"],
            keywords=["取消", "预约", "明天"],
            agent_url="mock://cancel_appointment",
            dispatch_priority=80,
            primary_threshold=0.62,
            candidate_threshold=0.42,
        ),
        IntentDefinition(
            intent_code="transfer_money",
            name="转账",
            description="执行转账",
            examples=["给张三转 200 元"],
            keywords=["转账", "付款", "账户"],
            agent_url="mock://transfer_money",
            dispatch_priority=95,
            primary_threshold=0.72,
            candidate_threshold=0.5,
        ),
    ]


def _test_app_with_mock_orchestrator() -> tuple[object, RouterOrchestrator]:
    orchestrator = RouterOrchestrator(
        publish_event=lambda event: None,
        intent_catalog=_StaticCatalog(_mock_intents()),
        agent_client=MockStreamingAgentClient(),
    )
    app = create_router_app()
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return app, orchestrator


def test_multi_intent_serial_flow_waits_then_resumes() -> None:
    async def run() -> None:
        app, _ = _test_app_with_mock_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            create_response = await client.post("/api/router/sessions")
            assert create_response.status_code == 201
            session_id = create_response.json()["session_id"]

            first_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "帮我查下订单，再帮我取消明天的预约"},
            )
            assert first_turn.status_code == 200

            snapshot = first_turn.json()["snapshot"]
            tasks = snapshot["tasks"]
            assert len(tasks) >= 2
            assert tasks[0]["status"] == "waiting_user_input"
            assert tasks[1]["status"] == "queued"

            second_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "订单号 123"},
            )
            assert second_turn.status_code == 200

            snapshot = second_turn.json()["snapshot"]
            task_by_intent = {task["intent_code"]: task for task in snapshot["tasks"]}
            assert task_by_intent["query_order_status"]["status"] == "completed"
            assert task_by_intent["cancel_appointment"]["status"] == "completed"

    asyncio.run(run())


def test_router_health_supports_ingress_prefix() -> None:
    async def run() -> None:
        app, _ = _test_app_with_mock_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/router/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    asyncio.run(run())


def test_message_snapshot_contains_agent_reply_without_waiting_for_sse() -> None:
    async def run() -> None:
        app, _ = _test_app_with_mock_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/sessions")).json()["session_id"]

            response = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "帮我查下订单状态"},
            )
            assert response.status_code == 200

            snapshot = response.json()["snapshot"]
            messages = snapshot["messages"]
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "帮我查下订单状态"
            assert messages[1]["role"] == "assistant"
            assert messages[1]["content"] == "请提供订单号"

    asyncio.run(run())


def test_stream_message_endpoint_emits_sse_events_then_snapshot() -> None:
    async def run() -> None:
        broker = EventBroker()
        orchestrator = RouterOrchestrator(
            publish_event=broker.publish,
            intent_catalog=_StaticCatalog(_mock_intents()),
            agent_client=MockStreamingAgentClient(),
        )
        app = create_router_app()
        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
        from router_api.dependencies import get_event_broker
        app.dependency_overrides[get_event_broker] = lambda: broker
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/sessions")).json()["session_id"]

            async with client.stream(
                "POST",
                f"/api/router/sessions/{session_id}/messages/stream",
                json={"content": "帮我查下订单状态"},
                headers={"Accept": "text/event-stream"},
            ) as response:
                assert response.status_code == 200
                body = ""
                async for chunk in response.aiter_text():
                    body += chunk

            assert "event: task.waiting_user_input" in body
            assert "请提供订单号" in body
            assert "event: snapshot" not in body

    asyncio.run(run())


def test_transfer_task_supports_multiple_rounds() -> None:
    async def run() -> None:
        app, _ = _test_app_with_mock_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/sessions")).json()["session_id"]

            start = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "帮我转账给张三"},
            )
            assert start.status_code == 200
            task = start.json()["snapshot"]["tasks"][0]
            assert task["status"] == "waiting_user_input"

            amount_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "200 元"},
            )
            assert amount_turn.status_code == 200
            task = amount_turn.json()["snapshot"]["tasks"][0]
            assert task["status"] == "waiting_user_input"

            final_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "使用工资卡"},
            )
            assert final_turn.status_code == 200
            task = final_turn.json()["snapshot"]["tasks"][0]
            assert task["status"] == "completed"

    asyncio.run(run())


def test_expired_session_promotes_short_term_memory_to_customer_memory() -> None:
    async def run() -> None:
        app, orchestrator = _test_app_with_mock_orchestrator()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            create_response = await client.post("/api/router/sessions", json={"cust_id": "cust_001"})
            session_id = create_response.json()["session_id"]

            first_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "帮我转账给张三", "cust_id": "cust_001"},
            )
            assert first_turn.status_code == 200

            session = orchestrator.session_store.get(session_id)
            session.expires_at = utc_now() - timedelta(minutes=1)

            second_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "帮我查下订单", "cust_id": "cust_001"},
            )
            assert second_turn.status_code == 200
            snapshot = second_turn.json()["snapshot"]
            assert snapshot["cust_id"] == "cust_001"
            assert snapshot["messages"][0]["content"] == "帮我查下订单"

            long_term_memory = orchestrator.session_store.long_term_memory.recall("cust_001")
            assert any("帮我转账给张三" in item for item in long_term_memory)

    asyncio.run(run())


def test_multi_intent_serial_flow_over_http_agents_uses_history_on_resumed_turn() -> None:
    class StaticCatalog:
        def __init__(self) -> None:
            self._intents = [
                IntentDefinition(
                    intent_code="query_order_status",
                    name="查询订单状态",
                    description="查询订单状态",
                    examples=["帮我查下订单状态"],
                    keywords=["订单", "状态", "查询"],
                    agent_url="http://intent-order-agent/api/agent/run",
                    dispatch_priority=100,
                    primary_threshold=0.68,
                    candidate_threshold=0.45,
                    request_schema={
                        "type": "object",
                        "required": ["sessionId", "taskId", "input", "conversation.recentMessages"],
                    },
                    field_mapping={
                        "sessionId": "$session.id",
                        "taskId": "$task.id",
                        "input": "$message.current",
                        "conversation.recentMessages": "$context.recent_messages",
                        "conversation.longTermMemory": "$context.long_term_memory",
                        "order.orderId": "$slot_memory.order_id",
                    },
                ),
                IntentDefinition(
                    intent_code="cancel_appointment",
                    name="取消预约",
                    description="取消预约",
                    examples=["帮我取消明天的预约"],
                    keywords=["取消", "预约", "明天"],
                    agent_url="http://intent-appointment-agent/api/agent/run",
                    dispatch_priority=80,
                    primary_threshold=0.62,
                    candidate_threshold=0.42,
                    request_schema={
                        "type": "object",
                        "required": ["sessionId", "taskId", "input", "conversation.recentMessages"],
                    },
                    field_mapping={
                        "sessionId": "$session.id",
                        "taskId": "$task.id",
                        "input": "$message.current",
                        "conversation.recentMessages": "$context.recent_messages",
                        "conversation.longTermMemory": "$context.long_term_memory",
                        "appointment.dateText": "$slot_memory.appointment_date",
                    },
                ),
            ]

        def list_active(self) -> list[IntentDefinition]:
            return list(self._intents)

        def priorities(self) -> dict[str, int]:
            return {intent.intent_code: intent.dispatch_priority for intent in self._intents}

    async def run() -> None:
        order_agent_inputs: list[dict[str, object]] = []
        appointment_agent_inputs: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            if request.url.host == "intent-order-agent":
                order_agent_inputs.append(payload)
                order_id = payload.get("order", {}).get("orderId")
                if not order_id:
                    content = str(payload.get("input", ""))
                    if "123" in content:
                        order_id = "123"
                if not order_id:
                    return httpx.Response(
                        200,
                        json={
                            "event": "message",
                            "content": "请提供订单号",
                            "ishandover": False,
                            "status": "waiting_user_input",
                            "payload": {"agent": "query_order_status"},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "event": "final",
                        "content": f"订单 {order_id} 当前状态为已发货",
                        "ishandover": True,
                        "status": "completed",
                        "slot_memory": {"order_id": order_id},
                        "payload": {"agent": "query_order_status", "order_id": order_id},
                    },
                )

            appointment_agent_inputs.append(payload)
            recent_messages = payload.get("conversation", {}).get("recentMessages", [])
            source_text = " ".join([str(payload.get("input", "")), *[str(item) for item in recent_messages]])
            if "明天" not in source_text:
                return httpx.Response(
                    200,
                    json={
                        "event": "message",
                        "content": "请提供要取消的预约时间",
                        "ishandover": False,
                        "status": "waiting_user_input",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "event": "final",
                    "content": "明天的预约已取消",
                    "ishandover": True,
                    "status": "completed",
                    "slot_memory": {"appointment_date": "明天"},
                    "payload": {"agent": "cancel_appointment", "appointment_date": "明天"},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            orchestrator = RouterOrchestrator(
                publish_event=lambda event: None,
                intent_catalog=StaticCatalog(),
                agent_client=StreamingAgentClient(http_client=http_client),
            )
            app = create_router_app()
            app.dependency_overrides[get_orchestrator] = lambda: orchestrator

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                session_id = (await client.post("/api/router/sessions")).json()["session_id"]

                first_turn = await client.post(
                    f"/api/router/sessions/{session_id}/messages",
                    json={"content": "帮我查下订单，再帮我取消明天的预约"},
                )
                assert first_turn.status_code == 200
                snapshot = first_turn.json()["snapshot"]
                task_by_intent = {task["intent_code"]: task for task in snapshot["tasks"]}
                assert task_by_intent["query_order_status"]["status"] == "waiting_user_input"
                assert task_by_intent["cancel_appointment"]["status"] == "queued"

                second_turn = await client.post(
                    f"/api/router/sessions/{session_id}/messages",
                    json={"content": "订单号 123"},
                )
                assert second_turn.status_code == 200
                snapshot = second_turn.json()["snapshot"]
                task_by_intent = {task["intent_code"]: task for task in snapshot["tasks"]}
                assert task_by_intent["query_order_status"]["status"] == "completed"
                assert task_by_intent["cancel_appointment"]["status"] == "completed"

        assert len(order_agent_inputs) == 2
        assert len(appointment_agent_inputs) == 1
        assert appointment_agent_inputs[0]["input"] == "订单号 123"
        assert any("明天" in item for item in appointment_agent_inputs[0]["conversation"]["recentMessages"])

    asyncio.run(run())
