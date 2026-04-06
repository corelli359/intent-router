from __future__ import annotations

import asyncio

import httpx

from router_api.app import create_router_app
from router_api.dependencies import get_event_broker_v2, get_orchestrator_v2
from router_api.sse.broker import EventBroker
from router_core.agent_client import MockStreamingAgentClient
from router_core.domain import IntentDefinition
from router_core.v2_orchestrator import GraphRouterOrchestrator


class _StaticCatalog:
    def __init__(self, intents: list[IntentDefinition]) -> None:
        self._intents = intents

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def get_fallback_intent(self) -> IntentDefinition | None:
        return next((intent for intent in self._intents if intent.is_fallback), None)


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
            examples=["给张三转 200 元", "帮我转账"],
            keywords=["转账", "付款", "汇款"],
            agent_url="mock://transfer_money",
            dispatch_priority=95,
            primary_threshold=0.72,
            candidate_threshold=0.5,
        ),
    ]


def _test_v2_app() -> tuple[object, GraphRouterOrchestrator]:
    broker = EventBroker()
    orchestrator = GraphRouterOrchestrator(
        publish_event=broker.publish,
        intent_catalog=_StaticCatalog(_mock_intents()),
        agent_client=MockStreamingAgentClient(),
    )
    app = create_router_app()
    app.dependency_overrides[get_orchestrator_v2] = lambda: orchestrator
    app.dependency_overrides[get_event_broker_v2] = lambda: broker
    return app, orchestrator


def test_v2_multi_intent_graph_requires_confirmation_and_runs_sequentially() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "先查余额，再给张三转账 200 元，卡号 6222020100049999999，尾号 1234"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["pending_graph"]["status"] == "waiting_confirmation"
            assert len(snapshot["pending_graph"]["nodes"]) == 2
            assert snapshot["pending_graph"]["edges"][0]["relation_type"] == "sequential"

            graph = snapshot["pending_graph"]
            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200
            confirmed_snapshot = confirm_turn.json()["snapshot"]
            assert confirmed_snapshot["current_graph"]["status"] in {
                "waiting_user_input",
                "partially_completed",
                "completed",
            }
            assert confirmed_snapshot["pending_graph"] is None

    asyncio.run(run())


def test_v2_waiting_node_switches_to_new_intent() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我转账"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["current_graph"]["status"] == "waiting_user_input"
            assert snapshot["current_graph"]["nodes"][0]["intent_code"] == "transfer_money"

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "算了，帮我查余额"},
            )
            assert second_turn.status_code == 200
            switched_snapshot = second_turn.json()["snapshot"]
            assert switched_snapshot["current_graph"]["nodes"][0]["intent_code"] == "query_account_balance"
            assert switched_snapshot["current_graph"]["status"] == "waiting_user_input"

    asyncio.run(run())


def test_v2_cancel_node_action_cancels_current_graph_node() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我转账"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            node_id = snapshot["current_graph"]["nodes"][0]["node_id"]

            cancel_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": node_id,
                    "source": "router",
                    "action_code": "cancel_node",
                    "payload": {"reason": "用户主动取消"},
                },
            )
            assert cancel_turn.status_code == 200
            cancelled_snapshot = cancel_turn.json()["snapshot"]
            assert cancelled_snapshot["current_graph"]["nodes"][0]["status"] == "cancelled"
            assert cancelled_snapshot["current_graph"]["status"] == "cancelled"

    asyncio.run(run())
