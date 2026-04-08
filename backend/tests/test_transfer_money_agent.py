from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from intent_agents.transfer_money_app import create_app, get_transfer_money_service  # noqa: E402
from intent_agents.transfer_money_service import TransferMoneyAgentRequest, TransferMoneyAgentService  # noqa: E402


class FakeJsonRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def run_json(self, *, prompt, variables: dict[str, Any], schema=None) -> Any:
        return self.payload


def test_transfer_money_service_waits_with_semantic_partial_slots() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": "李四",
                    "recipient_card_number": None,
                    "recipient_phone_last4": None,
                    "amount": "5000",
                    "has_enough_information": False,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_001",
                taskId="task_transfer_001",
                input="帮我给李四转 5000 元",
                conversation={
                    "recentMessages": ["user: 帮我给李四转 5000 元"],
                    "longTermMemory": [
                        "query_account_balance: card_number=6222021234567890, phone_last_four=1234"
                    ],
                },
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请提供收款卡号、收款人手机号后4位"
        assert response.slot_memory == {
            "recipient_name": "李四",
            "amount": "5000",
        }

    asyncio.run(run())


def test_transfer_money_service_completes_after_follow_up_details() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": None,
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last4": "1234",
                    "amount": None,
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_002",
                taskId="task_transfer_002",
                input="收款卡号 6222020100049999999，收款人手机号后四位 1234",
                recipient={"name": "李四"},
                transfer={"amount": "5000"},
                conversation={
                    "recentMessages": [
                        "user: 帮我给李四转 5000 元",
                        "assistant: 请提供收款卡号、收款人手机号后4位",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "completed"
        assert response.content == "已向李四转账 5000 元，转账成功"
        assert response.slot_memory == {
            "recipient_name": "李四",
            "recipient_card_number": "6222020100049999999",
            "recipient_phone_last_four": "1234",
            "amount": "5000",
        }

    asyncio.run(run())


def test_transfer_money_service_preserves_existing_slots_when_prompt_only_returns_new_detail() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": None,
                    "recipient_card_number": None,
                    "recipient_phone_last4": "1234",
                    "amount": None,
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_003",
                taskId="task_transfer_003",
                input="1234",
                recipient={
                    "name": "李四",
                    "cardNumber": "6222020100049999999",
                },
                transfer={"amount": "5000"},
                conversation={
                    "recentMessages": [
                        "user: 帮我给李四转 5000 元",
                        "assistant: 请提供收款人手机号后4位",
                        "user: 1234",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "completed"
        assert response.slot_memory == {
            "recipient_name": "李四",
            "recipient_card_number": "6222020100049999999",
            "recipient_phone_last_four": "1234",
            "amount": "5000",
        }

    asyncio.run(run())


def test_transfer_money_service_uses_prompt_ask_message_for_ambiguous_four_digits() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": None,
                    "recipient_card_number": None,
                    "recipient_phone_last4": None,
                    "amount": None,
                    "has_enough_information": False,
                    "ask_message": (
                        "检测到 4 位数字，请明确这是收款人手机号后4位还是转账金额，"
                        "例如“手机号后四位 1234”或“转账金额 5000”"
                    ),
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_004",
                taskId="task_transfer_004",
                input="1234",
                recipient={
                    "name": "李四",
                    "cardNumber": "6222020100049999999",
                },
                conversation={
                    "recentMessages": [
                        "user: 帮我给李四转账",
                        "assistant: 请提供收款人手机号后4位、转账金额",
                        "user: 1234",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == (
            "检测到 4 位数字，请明确这是收款人手机号后4位还是转账金额，"
            "例如“手机号后四位 1234”或“转账金额 5000”"
        )
        assert response.slot_memory == {
            "recipient_name": "李四",
            "recipient_card_number": "6222020100049999999",
        }

    asyncio.run(run())


def test_transfer_money_service_accepts_standalone_amount_when_only_amount_is_missing() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": None,
                    "recipient_card_number": None,
                    "recipient_phone_last4": None,
                    "amount": "5000",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_005",
                taskId="task_transfer_005",
                input="5000",
                recipient={
                    "name": "李四",
                    "cardNumber": "6222020100049999999",
                    "phoneLast4": "1234",
                },
                conversation={
                    "recentMessages": [
                        "user: 帮我给李四转账",
                        "assistant: 请提供转账金额",
                        "user: 5000",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "completed"
        assert response.slot_memory["amount"] == "5000"
        assert response.content == "已向李四转账 5000 元，转账成功"

    asyncio.run(run())


def test_transfer_money_service_accepts_numeric_amount_from_request_slots() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": None,
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last4": "1234",
                    "amount": None,
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_006",
                taskId="task_transfer_006",
                input="收款卡号 6222020100049999999，收款人手机号后四位 1234",
                recipient={"name": "我媳妇儿"},
                transfer={"amount": 1000},
                conversation={
                    "recentMessages": [
                        "user: 帮我查一下余额，如果超过5000，就跟我媳妇儿转1000",
                        "assistant: 请提供收款卡号、收款人手机号后4位",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "completed"
        assert response.slot_memory["amount"] == "1000"
        assert response.content == "已向我媳妇儿转账 1000 元，转账成功"

    asyncio.run(run())


def test_transfer_money_service_fails_when_amount_exceeds_limit() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": "李四",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last4": "1234",
                    "amount": "12000",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_006",
                taskId="task_transfer_006",
                input="给李四转 12000 元",
                recipient={
                    "name": "李四",
                    "cardNumber": "6222020100049999999",
                    "phoneLast4": "1234",
                },
                transfer={"amount": "12000"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "failed"
        assert response.content == "账户余额不足"

    asyncio.run(run())


def test_transfer_money_service_does_not_silently_reuse_history_sensitive_slots() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": "小明",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last4": "1234",
                    "amount": "1000",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_007",
                taskId="task_transfer_007",
                input="帮我转账",
                conversation={
                    "recentMessages": [
                        "user: 给小明转1000",
                        "assistant: 请提供收款卡号、收款人手机号后4位",
                        "user: 卡号 6222020100049999999，后四位 1234",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请提供收款人姓名、收款卡号、收款人手机号后4位、转账金额"
        assert response.slot_memory == {}

    asyncio.run(run())


def test_transfer_money_http_app_returns_router_payload() -> None:
    async def run() -> None:
        app = create_app()
        app.dependency_overrides[get_transfer_money_service] = lambda: TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "recipient_name": "李四",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last4": "1234",
                    "amount": "3000",
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
                    "sessionId": "session_transfer_007",
                    "taskId": "task_transfer_007",
                    "input": "给李四转 3000 元",
                    "recipient": {
                        "name": "李四",
                        "cardNumber": "6222020100049999999",
                        "phoneLast4": "1234",
                    },
                    "transfer": {"amount": "3000"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["slot_memory"]["amount"] == "3000"

    asyncio.run(run())
