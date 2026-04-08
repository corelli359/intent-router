from __future__ import annotations

import re
from collections.abc import AsyncIterator

from router_core.domain import AgentStreamChunk, Task, TaskStatus


CARD_RE = re.compile(r"\b(\d{12,19})\b")
PHONE_LAST4_RE = re.compile(r"(?:后4位|后四位|尾号)\D*(\d{4})")
FOUR_DIGITS_ONLY_RE = re.compile(r"^\D*(\d{4})\D*$")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
NAME_RE = re.compile(
    r"(?:给|向|转给|转账给)([\u4e00-\u9fffA-Za-z]{2,16}?)(?=(?:转账|转|汇款|付款|支付|卡号|银行卡|手机号|尾号|后4位|后四位|金额|[，,。\s]|$))"
)


class MockStreamingAgentClient:
    """Test-only agent simulator. Production routing must dispatch over HTTP."""

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        intent = task.intent_code
        if intent == "query_account_balance":
            yield self._handle_account_balance(task, user_input)
            return
        if intent == "update_shipping_address":
            yield self._handle_address(task, user_input)
            return
        if intent == "transfer_money":
            async for chunk in self._handle_transfer(task, user_input):
                yield chunk
            return
        if intent == "query_credit_card_repayment":
            yield self._handle_credit_card_repayment(task, user_input)
            return
        if intent == "pay_gas_bill":
            yield self._handle_gas_bill(task, user_input)
            return
        if intent == "exchange_forex":
            yield self._handle_exchange_forex(task, user_input)
            return
        if intent == "pay_bill":
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="缴费任务已创建，待接入真实缴费 Agent",
                ishandover=True,
                status=TaskStatus.COMPLETED,
            )
            return
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"{intent} 暂无模拟实现",
            ishandover=True,
            status=TaskStatus.FAILED,
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        return None

    async def close(self) -> None:
        return None

    def _handle_account_balance(self, task: Task, user_input: str) -> AgentStreamChunk:
        card = self._extract_card_number(user_input)
        phone_last4 = self._extract_phone_last4(user_input)
        if card:
            task.slot_memory["card_number"] = card
        if phone_last4:
            task.slot_memory["phone_last_four"] = phone_last4

        if "card_number" not in task.slot_memory and "phone_last_four" not in task.slot_memory:
            message = "请提供卡号和手机号后4位"
        elif "card_number" not in task.slot_memory:
            message = "请提供卡号"
        elif "phone_last_four" not in task.slot_memory:
            message = "请提供手机号后4位"
        else:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="查询成功，账户余额为 8000 元",
                ishandover=True,
                status=TaskStatus.COMPLETED,
                payload={"balance": 8000, **dict(task.slot_memory)},
            )

        return AgentStreamChunk(
            task_id=task.task_id,
            event="message",
            content=message,
            ishandover=False,
            status=TaskStatus.WAITING_USER_INPUT,
        )

    def _handle_address(self, task: Task, user_input: str) -> AgentStreamChunk:
        if "路" not in user_input and "区" not in user_input and "号" not in user_input:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请提供新的收货地址",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
        task.slot_memory["address"] = user_input
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content="地址已更新完成",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"address": user_input},
        )

    def _handle_credit_card_repayment(self, task: Task, user_input: str) -> AgentStreamChunk:
        card = self._extract_card_number(user_input)
        phone_last4 = self._extract_phone_last4(user_input)
        if card:
            task.slot_memory["card_number"] = card
        if phone_last4:
            task.slot_memory["phone_last_four"] = phone_last4

        if "card_number" not in task.slot_memory and "phone_last_four" not in task.slot_memory:
            message = "请提供信用卡卡号和手机号后4位"
        elif "card_number" not in task.slot_memory:
            message = "请提供信用卡卡号"
        elif "phone_last_four" not in task.slot_memory:
            message = "请提供手机号后4位"
        else:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="查询成功，本期应还 3200 元，最低还款 320 元，到期日 2026-04-25",
                ishandover=True,
                status=TaskStatus.COMPLETED,
                payload={"due_amount": 3200, "minimum_due": 320, **dict(task.slot_memory)},
            )

        return AgentStreamChunk(
            task_id=task.task_id,
            event="message",
            content=message,
            ishandover=False,
            status=TaskStatus.WAITING_USER_INPUT,
        )

    def _handle_gas_bill(self, task: Task, user_input: str) -> AgentStreamChunk:
        card = self._extract_card_number(user_input)
        if card:
            task.slot_memory["gas_account_number"] = card
        amount = self._extract_transfer_amount(user_input, task)
        if amount:
            task.slot_memory["amount"] = amount
        missing_fields: list[str] = []
        if "gas_account_number" not in task.slot_memory:
            missing_fields.append("燃气户号")
        if "amount" not in task.slot_memory:
            missing_fields.append("缴费金额")
        if missing_fields:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content=f"请提供{'、'.join(missing_fields)}",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已为燃气户号 {task.slot_memory['gas_account_number']} 缴费 {task.slot_memory['amount']} 元",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=dict(task.slot_memory),
        )

    def _handle_exchange_forex(self, task: Task, user_input: str) -> AgentStreamChunk:
        upper_text = user_input.upper()
        if "人民币" in user_input or "CNY" in upper_text:
            task.slot_memory.setdefault("sell_currency", "CNY")
        if "美元" in user_input or "USD" in upper_text:
            if "sell_currency" in task.slot_memory:
                task.slot_memory["buy_currency"] = "USD"
            else:
                task.slot_memory["sell_currency"] = "USD"
        amount = self._extract_transfer_amount(user_input, task)
        if amount:
            task.slot_memory["amount"] = amount
        missing_fields: list[str] = []
        if "sell_currency" not in task.slot_memory:
            missing_fields.append("卖出币种")
        if "buy_currency" not in task.slot_memory:
            missing_fields.append("买入币种")
        if "amount" not in task.slot_memory:
            missing_fields.append("换汇金额")
        if missing_fields:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content=f"请提供{'、'.join(missing_fields)}",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content="已提交换汇申请",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=dict(task.slot_memory),
        )

    async def _handle_transfer(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        name_match = NAME_RE.search(user_input)
        if not name_match:
            initial_source_input = self._initial_source_input(task)
            if initial_source_input:
                name_match = NAME_RE.search(initial_source_input)
        if name_match:
            task.slot_memory["recipient_name"] = name_match.group(1)
        card = self._extract_card_number(user_input)
        if card:
            task.slot_memory["recipient_card_number"] = card
        phone_last4 = self._extract_transfer_phone_last4(user_input, task)
        if phone_last4:
            task.slot_memory["recipient_phone_last_four"] = phone_last4
        amount = self._extract_transfer_amount(user_input, task)
        if not amount:
            initial_source_input = self._initial_source_input(task)
            if initial_source_input:
                amount = self._extract_transfer_amount(initial_source_input, task)
        if amount:
            task.slot_memory["amount"] = amount

        missing_fields: list[str] = []
        if "recipient_name" not in task.slot_memory:
            missing_fields.append("收款人姓名")
        if "recipient_card_number" not in task.slot_memory:
            missing_fields.append("收款卡号")
        if "recipient_phone_last_four" not in task.slot_memory:
            missing_fields.append("收款人手机号后4位")
        if "amount" not in task.slot_memory:
            missing_fields.append("转账金额")

        if missing_fields:
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content=f"请提供{'、'.join(missing_fields)}",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
            return

        amount = float(task.slot_memory["amount"])
        if amount > 8000:
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="账户余额不足",
                ishandover=True,
                status=TaskStatus.FAILED,
                payload=dict(task.slot_memory),
            )
            return

        amount_text = task.slot_memory["amount"]
        recipient_name = task.slot_memory.get("recipient_name", "收款人")
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已向{recipient_name}转账 {amount_text} 元，转账成功",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=dict(task.slot_memory),
        )

    def _extract_card_number(self, text: str) -> str | None:
        match = CARD_RE.search(text)
        return match.group(1) if match else None

    def _extract_phone_last4(self, text: str) -> str | None:
        match = PHONE_LAST4_RE.search(text)
        if match:
            return match.group(1)
        exact_match = FOUR_DIGITS_ONLY_RE.match(text.strip())
        if exact_match:
            return exact_match.group(1)
        return None

    def _extract_transfer_phone_last4(self, text: str, task: Task) -> str | None:
        match = PHONE_LAST4_RE.search(text)
        if match:
            return match.group(1)
        exact_match = FOUR_DIGITS_ONLY_RE.match(text.strip())
        if exact_match and "amount" in task.slot_memory and "recipient_phone_last_four" not in task.slot_memory:
            return exact_match.group(1)
        return None

    def _extract_transfer_amount(self, text: str, task: Task) -> str | None:
        amount_match = AMOUNT_RE.search(text)
        if amount_match:
            return amount_match.group(1)
        stripped = text.strip()
        if (
            stripped.isdigit()
            and "amount" not in task.slot_memory
            and "recipient_name" in task.slot_memory
            and "recipient_card_number" in task.slot_memory
            and "recipient_phone_last_four" in task.slot_memory
        ):
            return stripped
        return None

    def _initial_source_input(self, task: Task) -> str | None:
        value = task.input_context.get("initial_source_input")
        if isinstance(value, str) and value:
            return value
        return None
