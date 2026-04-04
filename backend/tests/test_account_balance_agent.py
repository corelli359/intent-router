from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from intent_agents.account_balance_app import create_app, get_account_balance_service  # noqa: E402
from intent_agents.account_balance_service import AccountBalanceAgentRequest, AccountBalanceAgentService  # noqa: E402


class FakeJsonRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def run_json(self, *, prompt, variables: dict[str, Any]) -> Any:
        return self.payload


def test_account_balance_service_waits_for_both_fields() -> None:
    async def run() -> None:
        service = AccountBalanceAgentService(resolver=None)
        response = await service.handle(
            AccountBalanceAgentRequest(
                sessionId="session_balance_001",
                taskId="task_balance_001",
                input="帮我查一下余额",
                conversation={"recentMessages": ["user: 帮我查一下余额"], "longTermMemory": []},
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请提供卡号和手机号后4位"

    asyncio.run(run())


def test_account_balance_service_heuristic_extracts_user_card_and_phone() -> None:
    async def run() -> None:
        service = AccountBalanceAgentService(resolver=None)
        response = await service.handle(
            AccountBalanceAgentRequest(
                sessionId="session_balance_005",
                taskId="task_balance_005",
                input="卡号 6222021234567890，手机号后四位 1234",
                conversation={"recentMessages": ["user: 卡号 6222021234567890 手机号后四位 1234"], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.slot_memory["card_number"] == "6222021234567890"
        assert response.slot_memory["phone_last_four"] == "1234"

    asyncio.run(run())


def test_account_balance_service_completes_with_card_and_phone() -> None:
    async def run() -> None:
        service = AccountBalanceAgentService(
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
            AccountBalanceAgentRequest(
                sessionId="session_balance_002",
                taskId="task_balance_002",
                input="卡号 6222021234567890，手机号后四位 1234",
                account={"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert "8000" in response.content
        assert response.slot_memory["card_number"] == "6222021234567890"
        assert response.slot_memory["phone_last_four"] == "1234"
        assert response.payload["balance"] == 8000

    asyncio.run(run())


def test_account_balance_service_ignores_transfer_memory_entries() -> None:
    async def run() -> None:
        service = AccountBalanceAgentService(resolver=None)
        response = await service.handle(
            AccountBalanceAgentRequest(
                sessionId="session_balance_004",
                taskId="task_balance_004",
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


def test_account_balance_http_app_returns_router_payload() -> None:
    async def run() -> None:
        app = create_app()
        app.dependency_overrides[get_account_balance_service] = lambda: AccountBalanceAgentService(resolver=None)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                json={
                    "sessionId": "session_balance_003",
                    "taskId": "task_balance_003",
                    "input": "卡号 6222021234567890，手机号后四位 1234",
                    "account": {"cardNumber": "6222021234567890", "phoneLast4": "1234"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["payload"]["balance"] == 8000
        assert payload["slot_memory"]["card_number"] == "6222021234567890"
        assert payload["slot_memory"]["phone_last_four"] == "1234"

    asyncio.run(run())
