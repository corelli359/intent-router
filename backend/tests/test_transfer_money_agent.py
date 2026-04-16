from __future__ import annotations

import asyncio
from typing import Any

import httpx

from transfer_money_agent.app import create_app, get_transfer_money_service  # noqa: E402
from transfer_money_agent.service import TransferMoneyAgentRequest, TransferMoneyAgentService  # noqa: E402


class FakeJsonRunner:
    """Small fake LLM runner used to control agent resolution outputs."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def run_json(self, *, prompt, variables: dict[str, Any], schema=None) -> Any:
        return self.payload


def test_transfer_money_service_waits_when_payee_identifier_is_missing() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": "500",
                    "payee_card_no": None,
                    "has_enough_information": False,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_001",
                taskId="task_transfer_001",
                input="帮我转账 500 元",
                conversation={"recentMessages": ["user: 帮我转账 500 元"], "longTermMemory": []},
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请提供收款人姓名"
        assert response.slot_memory == {"amount": "500"}

    asyncio.run(run())


def test_transfer_money_service_completes_with_business_required_slots_only() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": "5000",
                    "payee_name": "李四",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_002",
                taskId="task_transfer_002",
                input="给李四尾号8899那张卡转 5000 元人民币",
                conversation={"recentMessages": ["user: 给李四尾号8899那张卡转 5000 元人民币"], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.slot_memory == {
            "amount": "5000",
            "payee_name": "李四",
        }
        assert response.content == "已向李四转账 5000 CNY，转账成功"

    asyncio.run(run())


def test_transfer_money_service_preserves_existing_slots_when_prompt_only_returns_optional_detail() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": None,
                    "payee_card_no": None,
                    "payer_card_remark": "工资卡",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_003",
                taskId="task_transfer_003",
                input="付款卡备注工资卡",
                payee={"name": "李四", "cardNo": "8899"},
                transfer={"amount": "5000"},
                conversation={
                    "recentMessages": [
                        "user: 给李四尾号8899那张卡转 5000 元",
                        "assistant: 已识别到金额、收款人姓名和收款卡号",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "completed"
        assert response.slot_memory == {
            "amount": "5000",
            "payer_card_remark": "工资卡",
            "payee_name": "李四",
            "payee_card_no": "8899",
        }

    asyncio.run(run())


def test_transfer_money_service_does_not_silently_reuse_history_sensitive_slots() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": "1000",
                    "payee_name": "小明",
                    "payee_card_no": "5566",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_004",
                taskId="task_transfer_004",
                input="帮我转账",
                conversation={
                    "recentMessages": [
                        "user: 给小明转1000",
                        "assistant: 已向小明转账成功",
                    ],
                    "longTermMemory": [],
                },
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请提供金额、收款人姓名"
        assert response.slot_memory == {}

    asyncio.run(run())


def test_transfer_money_service_fails_when_amount_exceeds_limit() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": "12000",
                    "payee_name": "李四",
                    "payee_card_no": "8899",
                    "has_enough_information": True,
                    "ask_message": "",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_005",
                taskId="task_transfer_005",
                input="给李四尾号8899那张卡转 12000 元",
                payee={"name": "李四", "cardNo": "8899"},
                transfer={"amount": "12000"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "failed"
        assert response.content == "账户余额不足"

    asyncio.run(run())


def test_transfer_money_service_executes_directly_with_prefilled_slots_and_empty_input() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": None,
                    "payee_card_no": None,
                    "has_enough_information": False,
                    "ask_message": "不应调用 LLM 解析",
                }
            )
        )
        response = await service.handle(
            TransferMoneyAgentRequest(
                sessionId="session_transfer_006",
                taskId="task_transfer_006",
                input="",
                payee={"name": "李四", "cardNo": "8899"},
                transfer={"amount": "3000", "ccy": "USD"},
                conversation={"recentMessages": [], "longTermMemory": []},
            )
        )

        assert response.status == "completed"
        assert response.payload["business_status"] == "success"
        assert response.payload["payee_name"] == "李四"
        assert response.payload["payee_card_no"] == "8899"
        assert "ccy" not in response.slot_memory
        assert response.payload["ccy"] == "USD"

    asyncio.run(run())


def test_transfer_money_http_app_returns_router_payload() -> None:
    async def run() -> None:
        app = create_app()
        app.dependency_overrides[get_transfer_money_service] = lambda: TransferMoneyAgentService(
            resolver=FakeJsonRunner(
                {
                    "amount": "3000",
                    "payee_name": "李四",
                    "payee_card_no": "8899",
                    "ccy": "CNY",
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
                    "input": "给李四尾号8899那张卡转 3000 元",
                    "payee": {"name": "李四", "cardNo": "8899"},
                    "transfer": {"amount": "3000", "ccy": "CNY"},
                    "conversation": {"recentMessages": [], "longTermMemory": []},
                },
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "completed"
        assert payload["slot_memory"]["amount"] == "3000"
        assert payload["slot_memory"]["payee_name"] == "李四"
        assert payload["slot_memory"]["payee_card_no"] == "8899"

    asyncio.run(run())
