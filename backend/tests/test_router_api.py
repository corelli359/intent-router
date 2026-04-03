from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx

from router_api.dependencies import get_orchestrator
from router_api.app import create_router_app
from router_core.domain import utc_now
from router_core.orchestrator import RouterOrchestrator


def _test_app_with_mock_orchestrator() -> tuple[object, RouterOrchestrator]:
    orchestrator = RouterOrchestrator(publish_event=lambda event: None)
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
