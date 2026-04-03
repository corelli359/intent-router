from __future__ import annotations

import re
from collections.abc import AsyncIterator

from router_core.domain import AgentStreamChunk, Task, TaskStatus


ORDER_RE = re.compile(r"\b(\d{3,})\b")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")


class MockStreamingAgentClient:
    """Internal streaming agent simulator used before real agent URLs are wired in."""

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        intent = task.intent_code
        if intent == "query_order_status":
            yield self._handle_order(task, user_input)
            return
        if intent == "cancel_appointment":
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="明天的预约已取消",
                ishandover=True,
                status=TaskStatus.COMPLETED,
            )
            return
        if intent == "update_shipping_address":
            yield self._handle_address(task, user_input)
            return
        if intent == "transfer_money":
            async for chunk in self._handle_transfer(task, user_input):
                yield chunk
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

    def _handle_order(self, task: Task, user_input: str) -> AgentStreamChunk:
        match = ORDER_RE.search(user_input)
        if match is None:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请提供订单号",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
        order_id = match.group(1)
        task.slot_memory["order_id"] = order_id
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"订单 {order_id} 当前状态为已发货",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"order_id": order_id},
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

    async def _handle_transfer(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        if "张三" in user_input or "李四" in user_input:
            task.slot_memory.setdefault("payee", "张三" if "张三" in user_input else "李四")
        amount_match = AMOUNT_RE.search(user_input)
        if amount_match:
            task.slot_memory["amount"] = amount_match.group(1)
        if "工资卡" in user_input or "储蓄卡" in user_input:
            task.slot_memory["funding_account"] = user_input

        if "amount" not in task.slot_memory:
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请提供转账金额",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
            return
        if "funding_account" not in task.slot_memory:
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请确认付款账户",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
            return
        amount = task.slot_memory["amount"]
        payee = task.slot_memory.get("payee", "收款人")
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已完成向 {payee} 转账 {amount} 元",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=dict(task.slot_memory),
        )

