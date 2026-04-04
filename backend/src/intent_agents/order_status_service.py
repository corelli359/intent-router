from __future__ import annotations

import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from intent_agents.common import (
    AgentConversationContext,
    AgentCustomer,
    AgentExecutionResponse,
    JsonObjectRunner,
    dump_json,
)


ORDER_ID_RE = re.compile(r"\b(\d{3,})\b")


class OrderLookup(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    order_id: str | None = Field(default=None, alias="orderId")


class OrderStatusAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    order: OrderLookup = Field(default_factory=OrderLookup)


class OrderStatusResolution(BaseModel):
    order_id: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供订单号"


class OrderStatusAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: OrderStatusAgentRequest) -> AgentExecutionResponse:
        resolution = await self._resolve(request)
        slot_memory: dict[str, Any] = {}
        if resolution.order_id:
            normalized_order_id = self._normalize_order_id(resolution.order_id)
            if normalized_order_id:
                slot_memory["order_id"] = normalized_order_id
                return AgentExecutionResponse.completed(
                    f"订单 {normalized_order_id} 当前状态为已发货",
                    slot_memory=slot_memory,
                    payload={
                        "agent": "query_order_status",
                        "order_id": normalized_order_id,
                        "business_status": "shipped",
                    },
                )

        return AgentExecutionResponse.waiting(
            resolution.ask_message or "请提供订单号",
            slot_memory=slot_memory,
            payload={"agent": "query_order_status", "missing_fields": ["order_id"]},
        )

    async def _resolve(self, request: OrderStatusAgentRequest) -> OrderStatusResolution:
        seeded_order_id = self._seed_order_id(request)
        if self.resolver is None:
            return self._heuristic_resolution(seeded_order_id)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "你是订单状态服务里的参数解析器。"
                        "不要直接回答用户问题，只输出 JSON。"
                        "目标是判断是否已经拿到查询订单状态所需的 order_id。"
                        "如果无法可靠确认订单号，order_id 必须返回 null，并给出 ask_message。"
                    ),
                ),
                (
                    "human",
                    (
                        "当前输入:\n{input_text}\n\n"
                        "已知订单信息(JSON):\n{order_json}\n\n"
                        "最近对话(JSON):\n{recent_messages_json}\n\n"
                        "长期记忆(JSON):\n{long_term_memory_json}\n\n"
                        "请返回 JSON:\n"
                        "{\n"
                        '  "order_id": "string | null",\n'
                        '  "has_enough_information": true,\n'
                        '  "ask_message": "string"\n'
                        "}"
                    ),
                ),
            ]
        )

        try:
            raw_payload = await self.resolver.run_json(
                prompt=prompt,
                variables={
                    "input_text": request.input,
                    "order_json": dump_json(request.order.model_dump()),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                    "long_term_memory_json": dump_json(request.conversation.long_term_memory),
                },
            )
            resolution = OrderStatusResolution.model_validate(raw_payload)
        except Exception:
            return self._heuristic_resolution(seeded_order_id)

        if not resolution.order_id and seeded_order_id:
            resolution.order_id = seeded_order_id
        resolution.order_id = self._normalize_order_id(resolution.order_id)
        resolution.has_enough_information = bool(resolution.order_id)
        if not resolution.has_enough_information and not resolution.ask_message.strip():
            resolution.ask_message = "请提供订单号"
        return resolution

    def _heuristic_resolution(self, seeded_order_id: str | None) -> OrderStatusResolution:
        if seeded_order_id:
            return OrderStatusResolution(order_id=seeded_order_id, has_enough_information=True)
        return OrderStatusResolution(order_id=None, has_enough_information=False, ask_message="请提供订单号")

    def _seed_order_id(self, request: OrderStatusAgentRequest) -> str | None:
        for candidate in (
            request.order.order_id,
            self._extract_order_id(request.input),
            *[self._extract_order_id(item) for item in reversed(request.conversation.recent_messages)],
            *[self._extract_order_id(item) for item in reversed(request.conversation.long_term_memory)],
        ):
            normalized = self._normalize_order_id(candidate)
            if normalized:
                return normalized
        return None

    def _extract_order_id(self, text: str | None) -> str | None:
        if not text:
            return None
        match = ORDER_ID_RE.search(text)
        return match.group(1) if match else None

    def _normalize_order_id(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        match = ORDER_ID_RE.search(stripped)
        if match:
            return match.group(1)
        if stripped.isdigit() and len(stripped) >= 3:
            return stripped
        return None
