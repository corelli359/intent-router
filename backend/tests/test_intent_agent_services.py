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
from intent_agents.transfer_money_service import (
    TransferMoneyAgentRequest,
    TransferMoneyAgentService,
)


class FakeJsonRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def run_json(self, *, prompt, variables: dict[str, Any], schema=None) -> Any:
        return self.payload


def test_order_status_service_completes_when_balance_info_is_resolved() -> None:
    async def run() -> None:
        service = OrderStatusAgentService(
            resolver=FakeJsonRunner(
                {
                    "card_number": "6222021234567890",
                    "phone_last4": "1234",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            OrderStatusAgentRequest(
                sessionId="session_001",
                taskId="task_001",
                input="帮我查一下余额",
                account={"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                conversation={"recentMessages": ["user: 帮我查一下余额"], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["balance"] == 8000
        assert response.slot_memory["card_number"] == "6222021234567890"

    asyncio.run(run())


def test_order_status_service_ignores_transfer_memory_entries() -> None:
    async def run() -> None:
        service = OrderStatusAgentService(resolver=None)
        response = await service.handle(
            OrderStatusAgentRequest(
                sessionId="session_order_status_002",
                taskId="task_order_status_002",
                input="帮我查一下余额",
                conversation={
                    "recentMessages": ["user: 帮我查一下余额"],
                    "longTermMemory": [
                        "transfer_money: recipient_card_number=6222020100049999999, recipient_phone_last4=1234"
                    ],
                },
            )
        )

        assert response.status == "waiting_user_input"
        assert response.payload["missing_fields"] == ["card_number", "phone_last_four"]

    asyncio.run(run())


def test_cancel_appointment_service_waits_without_transfer_fields() -> None:
    async def run() -> None:
        service = CancelAppointmentAgentService()
        response = await service.handle(
            CancelAppointmentAgentRequest(
                sessionId="session_002",
                taskId="task_002",
                input="帮我转账",
                conversation={"recentMessages": ["user: 帮我转账"], "longTermMemory": []},
            )
        )

        assert response.status == "waiting_user_input"
        assert "收款人姓名" in response.content

    asyncio.run(run())


def test_transfer_money_service_requires_card_and_phone() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService()
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_003",
                taskId="task_003",
                input="帮我给李四转 5000 元",
                conversation={"recentMessages": ["user: 帮我给李四转 5000 元"], "longTermMemory": []},
            )
        )

        assert response.status == "waiting_user_input"
        assert "卡号" in response.content
        assert "手机号" in response.content

    asyncio.run(run())


def test_order_status_agent_http_app_returns_router_compatible_payload() -> None:
    async def run() -> None:
        app = create_order_status_app()
        app.dependency_overrides[get_order_status_service] = lambda: OrderStatusAgentService(resolver=None)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_003",
                    "taskId": "task_003",
                    "input": "卡号 6222021234567890，手机号后四位 1234",
                    "account": {"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                    "conversation": {"recentMessages": ["user: 查余额"], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["payload"]["balance"] == 8000

    asyncio.run(run())


def test_cancel_appointment_agent_http_app_returns_transfer_payload() -> None:
    async def run() -> None:
        app = create_cancel_appointment_app()
        app.dependency_overrides[get_cancel_appointment_service] = lambda: CancelAppointmentAgentService()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_004",
                    "taskId": "task_004",
                    "input": "给李四转 5000 元",
                    "recipient": {
                        "name": "李四",
                        "cardNumber": "6222020100049999999",
                        "phoneLast4": "1234",
                    },
                    "transfer": {"amount": "5000"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["slot_memory"]["recipient_name"] == "李四"

    asyncio.run(run())
