from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from transfer_money_agent.app import create_app, get_transfer_money_service  # noqa: E402
from transfer_money_agent.service import TransferMoneyAgentRequest, TransferMoneyAgentService  # noqa: E402


def _config_variables(values: dict[str, Any]) -> list[dict[str, str]]:
    variables: list[dict[str, str]] = []
    for name, value in values.items():
        if isinstance(value, str):
            serialized = value
        else:
            serialized = json.dumps(value, ensure_ascii=False)
        variables.append({"name": name, "value": serialized})
    return variables


class FakeJsonRunner:
    """Small fake LLM runner used to control agent resolution outputs."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def run_json(self, *, prompt, variables: dict[str, Any], schema=None) -> Any:
        self.calls.append({"prompt": prompt, "variables": variables, "schema": schema})
        return self.payload


def test_transfer_money_request_reads_slots_data_json_string() -> None:
    request = TransferMoneyAgentRequest(
        session_id="session_transfer_001",
        txt="给李四转账 500 元",
        stream=True,
        config_variables=_config_variables(
            {
                "slots_data": {
                    "amount": "500",
                    "payee_name": "李四",
                    "payee_card_no": "8899",
                }
            }
        ),
    )

    assert request.get_slots_data() == {
        "amount": "500",
        "payee_name": "李四",
        "payee_card_no": "8899",
    }


def test_transfer_money_service_prefers_config_variables_context_and_slots() -> None:
    async def run() -> None:
        runner = FakeJsonRunner(
            {
                "amount": None,
                "payee_name": None,
                "payer_card_remark": "工资卡",
                "has_enough_information": True,
                "ask_message": "",
            }
        )
        service = TransferMoneyAgentService(resolver=runner)
        response = await service.handle(
            TransferMoneyAgentRequest(
                session_id="session_transfer_002",
                txt="付款卡备注工资卡",
                stream=True,
                config_variables=_config_variables(
                    {
                        "intent": {
                            "code": "AG_TRANS",
                            "name": "立即发起一笔转账交易",
                            "description": "新契约上下文",
                        },
                        "recent_messages": [
                            "user: 给李四尾号8899那张卡转 5000 元",
                            "assistant: 已识别到金额、收款人姓名和收款卡号",
                        ],
                        "long_term_memory": ["用户经常给李四转账"],
                        "slots_data": {
                            "amount": "5000",
                            "payee_name": "李四",
                            "payee_card_no": "8899",
                        },
                    }
                ),
                intent={"code": "LEGACY_INTENT", "name": "旧意图"},
                conversation={
                    "recentMessages": ["legacy recent"],
                    "longTermMemory": ["legacy memory"],
                },
                payee={"name": "王五", "cardNo": "5566"},
                transfer={"amount": "9"},
            )
        )

        assert response.status == "completed"
        assert response.slot_memory == {
            "amount": "5000",
            "payer_card_remark": "工资卡",
            "payee_name": "李四",
            "payee_card_no": "8899",
        }
        assert response.content == "已受理向李四转账 5000 CNY，等待助手确认完成态"
        assert response.handOverReason == "等待助手确认完成态"
        assert response.completion_state == 1
        assert response.completion_reason == "agent_partial_done"

        assert len(runner.calls) == 1
        variables = runner.calls[0]["variables"]
        assert json.loads(variables["intent_json"]) == {
            "code": "AG_TRANS",
            "name": "立即发起一笔转账交易",
            "description": "新契约上下文",
            "examples": [],
        }
        assert json.loads(variables["recent_messages_json"]) == [
            "user: 给李四尾号8899那张卡转 5000 元",
            "assistant: 已识别到金额、收款人姓名和收款卡号",
        ]
        assert json.loads(variables["long_term_memory_json"]) == ["用户经常给李四转账"]
        assert json.loads(variables["current_slots_json"]) == {
            "amount": "5000",
            "ccy": None,
            "payer_card_no": None,
            "payer_card_remark": None,
            "payee_name": "李四",
            "payee_card_no": "8899",
            "payee_card_remark": None,
            "payee_card_bank": None,
            "payee_phone": None,
        }

    asyncio.run(run())


def test_transfer_money_service_accepts_legacy_nested_request_shape() -> None:
    async def run() -> None:
        service = TransferMoneyAgentService(resolver=None)
        request = TransferMoneyAgentRequest(
            sessionId="session_transfer_003",
            taskId="task_transfer_003",
            input="给李四转 500 元",
            payee={"name": "李四"},
            transfer={"amount": "500"},
            conversation={"recentMessages": [], "longTermMemory": []},
        )
        response = await service.handle(request)

        assert request.session_id == "session_transfer_003"
        assert request.txt == "给李四转 500 元"
        assert response.status == "completed"
        assert response.slot_memory == {
            "amount": "500",
            "payee_name": "李四",
        }
        assert response.content == "已受理向李四转账 500 CNY，等待助手确认完成态"
        assert response.completion_state == 1
        assert response.completion_reason == "agent_partial_done"

    asyncio.run(run())


def test_transfer_money_service_prioritizes_new_slots_over_legacy_nested_slots() -> None:
    async def run() -> None:
        runner = FakeJsonRunner(
            {
                "amount": "9999",
                "payee_name": "不会被使用",
                "has_enough_information": True,
                "ask_message": "",
            }
        )
        service = TransferMoneyAgentService(resolver=runner)
        response = await service.handle(
            TransferMoneyAgentRequest(
                session_id="session_transfer_004",
                txt="",
                stream=True,
                config_variables=_config_variables(
                    {
                        "slots_data": {
                            "amount": "3000",
                            "ccy": "USD",
                            "payee_name": "新收款人",
                            "payee_card_no": "8899",
                        }
                    }
                ),
                payee={"name": "旧收款人", "cardNo": "5566"},
                transfer={"amount": "100", "ccy": "CNY"},
            )
        )

        assert runner.calls == []
        assert response.status == "completed"
        assert response.content == "已受理向新收款人转账 3000 USD，等待助手确认完成态"
        assert response.handOverReason == "等待助手确认完成态"
        assert response.completion_state == 1
        assert response.completion_reason == "agent_partial_done"
        assert response.payload["amount"] == "3000"
        assert response.payload["ccy"] == "USD"
        assert response.payload["payee_name"] == "新收款人"
        assert response.payload["payee_card_no"] == "8899"

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
                session_id="session_transfer_005",
                txt="帮我转账",
                stream=True,
                config_variables=_config_variables(
                    {
                        "recent_messages": [
                            "user: 给小明转1000",
                            "assistant: 已向小明转账成功",
                        ],
                        "long_term_memory": [],
                    }
                ),
            )
        )

        assert response.status == "waiting_user_input"
        assert response.content == "请提供金额、收款人姓名"
        assert response.slot_memory == {}

    asyncio.run(run())


def test_transfer_money_http_app_streams_sse_contract() -> None:
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
            async with client.stream(
                "POST",
                "/api/agent/run",
                json={
                    "session_id": "session_transfer_006",
                    "txt": "给李四尾号8899那张卡转 3000 元",
                    "stream": True,
                    "config_variables": _config_variables(
                        {
                            "slots_data": {
                                "amount": "3000",
                                "payee_name": "李四",
                                "payee_card_no": "8899",
                                "ccy": "CNY",
                            }
                        }
                    ),
                },
            ) as response:
                lines = [line async for line in response.aiter_lines()]

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert lines.count("event:message") == 1
        assert lines.count("event:done") == 1

        message_line = lines[lines.index("event:message") + 1]
        done_line = lines[lines.index("event:done") + 1]
        assert message_line.startswith("data:")
        assert done_line == "data:[DONE]"

        payload = json.loads(message_line.removeprefix("data:"))
        assert payload["event"] == "final"
        assert payload["status"] == "completed"
        assert payload["slot_memory"] == {
            "amount": "3000",
            "payee_name": "李四",
            "payee_card_no": "8899",
        }
        assert payload["content"] == "已受理向李四转账 3000 CNY，等待助手确认完成态"
        assert payload["handOverReason"] == "等待助手确认完成态"
        assert payload["completion_state"] == 1
        assert payload["completion_reason"] == "agent_partial_done"

    asyncio.run(run())
