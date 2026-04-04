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
            intent_code="query_account_balance",
            name="查询账户余额",
            description="查询账户余额，需要卡号和手机号后4位。",
            examples=["帮我查一下账户余额", "查余额"],
            keywords=["余额", "账户", "银行卡"],
            agent_url="mock://query_account_balance",
            dispatch_priority=100,
            primary_threshold=0.68,
            candidate_threshold=0.45,
        ),
        IntentDefinition(
            intent_code="transfer_money",
            name="转账",
            description="执行转账，需要收款人姓名、收款卡号、手机号后4位和金额。",
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
                json={"content": "帮我查账户余额，再给张三转 200 元"},
            )
            assert first_turn.status_code == 200

            snapshot = first_turn.json()["snapshot"]
            tasks = snapshot["tasks"]
            assert len(tasks) >= 2
            assert tasks[0]["status"] == "waiting_user_input"
            assert tasks[1]["status"] == "queued"

            second_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "给张三转 200 元，卡号 6222021234567890，手机号后四位 1234"},
            )
            assert second_turn.status_code == 200

            snapshot = second_turn.json()["snapshot"]
            task_by_intent = {task["intent_code"]: task for task in snapshot["tasks"]}
            assert task_by_intent["query_account_balance"]["status"] == "completed"
            assert task_by_intent["transfer_money"]["status"] == "completed"

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
                json={"content": "帮我查一下账户余额"},
            )
            assert response.status_code == 200

            snapshot = response.json()["snapshot"]
            messages = snapshot["messages"]
            assert messages[0]["role"] == "user"
            assert messages[0]["content"] == "帮我查一下账户余额"
            assert messages[1]["role"] == "assistant"
            assert messages[1]["content"] == "请提供卡号和手机号后4位"

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
                json={"content": "帮我查一下账户余额"},
                headers={"Accept": "text/event-stream"},
            ) as response:
                assert response.status_code == 200
                body = ""
                async for chunk in response.aiter_text():
                    body += chunk

            assert "event: task.waiting_user_input" in body
            assert "请提供卡号和手机号后4位" in body
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
                json={"content": "帮我给张三转账 200 元"},
            )
            assert start.status_code == 200
            task = start.json()["snapshot"]["tasks"][0]
            assert task["status"] == "waiting_user_input"

            final_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "卡号 6222021111111111，手机号后四位 1234"},
            )
            assert final_turn.status_code == 200
            task = final_turn.json()["snapshot"]["tasks"][0]
            assert task["status"] == "completed"

    asyncio.run(run())


def test_transfer_resume_does_not_duplicate_tasks() -> None:
    async def run() -> None:
        app, _ = _test_app_with_mock_orchestrator()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/sessions")).json()["session_id"]

            first_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "帮我给张三转账", "cust_id": "cust_demo_001"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["tasks"][0]["status"] == "waiting_user_input"
            assert snapshot["tasks"][0]["intent_code"] == "transfer_money"

            second_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "5000 元", "cust_id": "cust_demo_001"},
            )
            assert second_turn.status_code == 200
            snapshot = second_turn.json()["snapshot"]
            assert len(snapshot["tasks"]) == 1

            third_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "工资卡", "cust_id": "cust_demo_001"},
            )
            assert third_turn.status_code == 200
            snapshot = third_turn.json()["snapshot"]
            assert len(snapshot["tasks"]) == 1

            fourth_turn = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "确认", "cust_id": "cust_demo_001"},
            )
            assert fourth_turn.status_code == 200
            snapshot = fourth_turn.json()["snapshot"]
            assert len(snapshot["tasks"]) == 1
            task = snapshot["tasks"][0]
            assert task["intent_code"] == "transfer_money"

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
                json={"content": "帮我查下账户余额", "cust_id": "cust_001"},
            )
            assert second_turn.status_code == 200
            snapshot = second_turn.json()["snapshot"]
            assert snapshot["cust_id"] == "cust_001"
            assert snapshot["messages"][0]["content"] == "帮我查下账户余额"

            long_term_memory = orchestrator.session_store.long_term_memory.recall("cust_001")
            assert any("帮我转账给张三" in item for item in long_term_memory)

    asyncio.run(run())


def test_multi_intent_serial_flow_over_http_agents_uses_history_on_resumed_turn() -> None:
    class StaticCatalog:
        def __init__(self) -> None:
            self._intents = [
                IntentDefinition(
                    intent_code="query_account_balance",
                    name="查询账户余额",
                    description="查询账户余额",
                    examples=["帮我查一下账户余额"],
                    keywords=["余额", "账户", "银行卡"],
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
                        "account.cardNumber": "$slot_memory.card_number",
                        "account.phoneLast4": "$slot_memory.phone_last_four",
                    },
                ),
                IntentDefinition(
                    intent_code="transfer_money",
                    name="转账",
                    description="转账",
                    examples=["给张三转 200 元"],
                    keywords=["转账", "张三", "元"],
                    agent_url="http://intent-appointment-agent/api/agent/run",
                    dispatch_priority=95,
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
                        "recipient.name": "$slot_memory.recipient_name",
                        "recipient.cardNumber": "$slot_memory.recipient_card_number",
                        "recipient.phoneLast4": "$slot_memory.recipient_phone_last_four",
                        "transfer.amount": "$slot_memory.amount",
                    },
                ),
            ]

        def list_active(self) -> list[IntentDefinition]:
            return list(self._intents)

        def priorities(self) -> dict[str, int]:
            return {intent.intent_code: intent.dispatch_priority for intent in self._intents}

    async def run() -> None:
        balance_agent_inputs: list[dict[str, object]] = []
        transfer_agent_inputs: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            if request.url.host == "intent-order-agent":
                balance_agent_inputs.append(payload)
                account = payload.get("account", {})
                card_number = account.get("cardNumber")
                phone_last4 = account.get("phoneLast4")
                content = str(payload.get("input", ""))
                if not card_number and "6222021234567890" in content:
                    card_number = "6222021234567890"
                if not phone_last4 and "1234" in content:
                    phone_last4 = "1234"
                if not (card_number and phone_last4):
                    return httpx.Response(
                        200,
                        json={
                            "event": "message",
                            "content": "请提供卡号和手机号后4位",
                            "ishandover": False,
                            "status": "waiting_user_input",
                            "payload": {"agent": "query_account_balance"},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "event": "final",
                        "content": "查询成功，账户余额为 8000 元",
                        "ishandover": True,
                        "status": "completed",
                        "slot_memory": {"card_number": card_number, "phone_last_four": phone_last4},
                        "payload": {"agent": "query_account_balance", "balance": 8000},
                    },
                )

            transfer_agent_inputs.append(payload)
            recent_messages = payload.get("conversation", {}).get("recentMessages", [])
            source_text = " ".join([str(payload.get("input", "")), *[str(item) for item in recent_messages]])
            recipient = payload.get("recipient", {})
            transfer = payload.get("transfer", {})
            recipient_name = recipient.get("name") or ("张三" if "张三" in source_text else None)
            recipient_card_number = recipient.get("cardNumber") or ("6222021234567890" if "6222021234567890" in source_text else None)
            recipient_phone_last4 = recipient.get("phoneLast4") or ("1234" if "1234" in source_text else None)
            amount = transfer.get("amount") or ("200" if "200" in source_text else None)
            if not (recipient_name and recipient_card_number and recipient_phone_last4 and amount):
                return httpx.Response(
                    200,
                    json={
                        "event": "message",
                        "content": "请提供收款人姓名、收款卡号、收款人手机号后4位、转账金额",
                        "ishandover": False,
                        "status": "waiting_user_input",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "event": "final",
                    "content": "已向张三转账 200 元，转账成功",
                    "ishandover": True,
                    "status": "completed",
                    "slot_memory": {
                        "recipient_name": recipient_name,
                        "recipient_card_number": recipient_card_number,
                        "recipient_phone_last_four": recipient_phone_last4,
                        "amount": amount,
                    },
                    "payload": {"agent": "transfer_money", "amount": amount},
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
                    json={"content": "帮我查账户余额，再给张三转 200 元"},
                )
                assert first_turn.status_code == 200
                snapshot = first_turn.json()["snapshot"]
                task_by_intent = {task["intent_code"]: task for task in snapshot["tasks"]}
                assert task_by_intent["query_account_balance"]["status"] == "waiting_user_input"
                assert task_by_intent["transfer_money"]["status"] == "queued"

                second_turn = await client.post(
                    f"/api/router/sessions/{session_id}/messages",
                    json={"content": "卡号 6222021234567890，手机号后四位 1234"},
                )
                assert second_turn.status_code == 200
                snapshot = second_turn.json()["snapshot"]
                task_by_intent = {task["intent_code"]: task for task in snapshot["tasks"]}
                assert task_by_intent["query_account_balance"]["status"] == "completed"
                assert task_by_intent["transfer_money"]["status"] == "completed"

        assert len(balance_agent_inputs) == 2
        assert len(transfer_agent_inputs) == 1
        assert transfer_agent_inputs[0]["input"] == "卡号 6222021234567890，手机号后四位 1234"
        assert any("张三" in item for item in transfer_agent_inputs[0]["conversation"]["recentMessages"])

    asyncio.run(run())
