from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from intent_agents.cancel_appointment_app import (  # noqa: E402
    create_app as create_cancel_appointment_app,
    get_cancel_appointment_service,
)
from intent_agents.cancel_appointment_service import (  # noqa: E402
    CancelAppointmentAgentRequest,
    CancelAppointmentAgentService,
)
from intent_agents.order_status_app import create_app as create_order_status_app  # noqa: E402
from intent_agents.order_status_app import get_order_status_service  # noqa: E402
from intent_agents.order_status_service import OrderStatusAgentRequest, OrderStatusAgentService  # noqa: E402


class FakeJsonRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def run_json(self, *, prompt, variables: dict[str, Any]) -> Any:
        self.calls.append(variables)
        return self.payload


def test_order_status_service_completes_when_order_id_is_resolved() -> None:
    async def run() -> None:
        service = OrderStatusAgentService(
            resolver=FakeJsonRunner(
                {
                    "order_id": "12345",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            OrderStatusAgentRequest(
                sessionId="session_001",
                taskId="task_001",
                input="帮我查一下订单 12345",
                conversation={"recentMessages": ["user: 帮我查一下订单 12345"]},
            )
        )

        assert response.status == "completed"
        assert response.slot_memory["order_id"] == "12345"
        assert response.payload["business_status"] == "shipped"

    asyncio.run(run())


def test_cancel_appointment_service_waits_without_date_or_reference() -> None:
    async def run() -> None:
        service = CancelAppointmentAgentService(
            resolver=FakeJsonRunner(
                {
                    "appointment_date": None,
                    "booking_reference": None,
                    "has_enough_information": False,
                    "ask_message": "请告诉我要取消哪一天的预约",
                }
            )
        )
        response = await service.handle(
            CancelAppointmentAgentRequest(
                sessionId="session_002",
                taskId="task_002",
                input="帮我取消预约",
                conversation={"recentMessages": ["user: 帮我取消预约"]},
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请告诉我要取消哪一天的预约"

    asyncio.run(run())


def test_order_status_agent_http_app_returns_router_compatible_payload() -> None:
    async def run() -> None:
        app = create_order_status_app()
        app.dependency_overrides[get_order_status_service] = lambda: OrderStatusAgentService(
            resolver=FakeJsonRunner(
                {
                    "order_id": "9988",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_003",
                    "taskId": "task_003",
                    "input": "查订单 9988",
                    "conversation": {"recentMessages": ["user: 查订单 9988"], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["slot_memory"]["order_id"] == "9988"

    asyncio.run(run())


def test_cancel_appointment_agent_http_app_uses_history_for_completion() -> None:
    async def run() -> None:
        app = create_cancel_appointment_app()
        app.dependency_overrides[get_cancel_appointment_service] = lambda: CancelAppointmentAgentService(
            resolver=FakeJsonRunner(
                {
                    "appointment_date": "明天",
                    "booking_reference": None,
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_004",
                    "taskId": "task_004",
                    "input": "订单号 123",
                    "conversation": {
                        "recentMessages": ["user: 帮我查下订单，再帮我取消明天的预约", "user: 订单号 123"],
                        "longTermMemory": [],
                    },
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["payload"]["appointment_date"] == "明天"

    asyncio.run(run())
