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


BOOKING_REFERENCE_RE = re.compile(r"\b[A-Za-z0-9]{6,}\b")
DATE_TOKEN_RE = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}月\d{1,2}日|\d{1,2}[-/]\d{1,2}|\b今天\b|\b明天\b|\b后天\b)"
)


class AppointmentSelection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_text: str | None = Field(default=None, alias="dateText")
    booking_reference: str | None = Field(default=None, alias="bookingReference")


class CancelAppointmentAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    appointment: AppointmentSelection = Field(default_factory=AppointmentSelection)


class CancelAppointmentResolution(BaseModel):
    appointment_date: str | None = None
    booking_reference: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供要取消的预约时间或预约编号"


class CancelAppointmentAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: CancelAppointmentAgentRequest) -> AgentExecutionResponse:
        resolution = await self._resolve(request)
        slot_memory: dict[str, Any] = {}
        if resolution.appointment_date:
            slot_memory["appointment_date"] = resolution.appointment_date
        if resolution.booking_reference:
            slot_memory["booking_reference"] = resolution.booking_reference

        if resolution.has_enough_information:
            if resolution.booking_reference:
                content = f"预约 {resolution.booking_reference} 已取消"
            else:
                content = f"{resolution.appointment_date}的预约已取消"
            return AgentExecutionResponse.completed(
                content,
                slot_memory=slot_memory,
                payload={
                    "agent": "cancel_appointment",
                    "appointment_date": resolution.appointment_date,
                    "booking_reference": resolution.booking_reference,
                    "business_status": "cancelled",
                },
            )

        return AgentExecutionResponse.waiting(
            resolution.ask_message or "请提供要取消的预约时间或预约编号",
            slot_memory=slot_memory,
            payload={
                "agent": "cancel_appointment",
                "missing_fields": ["appointment_date_or_booking_reference"],
            },
        )

    async def _resolve(self, request: CancelAppointmentAgentRequest) -> CancelAppointmentResolution:
        seeded_date, seeded_reference = self._seed_context(request)
        if self.resolver is None:
            return self._heuristic_resolution(seeded_date, seeded_reference)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "你是预约取消服务里的参数解析器。"
                        "不要直接回答用户问题，只输出 JSON。"
                        "目标是判断是否已经拿到取消预约所需的信息。"
                        "允许用 appointment_date 或 booking_reference 任意一种作为完成条件。"
                        "如果信息不够，不要猜测不存在的编号或日期。"
                    ),
                ),
                (
                    "human",
                    (
                        "当前输入:\n{input_text}\n\n"
                        "已知预约信息(JSON):\n{appointment_json}\n\n"
                        "最近对话(JSON):\n{recent_messages_json}\n\n"
                        "长期记忆(JSON):\n{long_term_memory_json}\n\n"
                        "请返回 JSON:\n"
                        "{\n"
                        '  "appointment_date": "string | null",\n'
                        '  "booking_reference": "string | null",\n'
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
                    "appointment_json": dump_json(request.appointment.model_dump()),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                    "long_term_memory_json": dump_json(request.conversation.long_term_memory),
                },
            )
            resolution = CancelAppointmentResolution.model_validate(raw_payload)
        except Exception:
            return self._heuristic_resolution(seeded_date, seeded_reference)

        if not resolution.appointment_date and seeded_date:
            resolution.appointment_date = seeded_date
        if not resolution.booking_reference and seeded_reference:
            resolution.booking_reference = seeded_reference
        resolution.appointment_date = self._normalize_date_text(resolution.appointment_date)
        resolution.booking_reference = self._normalize_booking_reference(resolution.booking_reference)
        resolution.has_enough_information = bool(resolution.appointment_date or resolution.booking_reference)
        if not resolution.has_enough_information and not resolution.ask_message.strip():
            resolution.ask_message = "请提供要取消的预约时间或预约编号"
        return resolution

    def _heuristic_resolution(
        self,
        appointment_date: str | None,
        booking_reference: str | None,
    ) -> CancelAppointmentResolution:
        if appointment_date or booking_reference:
            return CancelAppointmentResolution(
                appointment_date=appointment_date,
                booking_reference=booking_reference,
                has_enough_information=True,
            )
        return CancelAppointmentResolution(
            appointment_date=None,
            booking_reference=None,
            has_enough_information=False,
            ask_message="请提供要取消的预约时间或预约编号",
        )

    def _seed_context(self, request: CancelAppointmentAgentRequest) -> tuple[str | None, str | None]:
        date_candidates = [
            request.appointment.date_text,
            self._extract_date_text(request.input),
            *[self._extract_date_text(item) for item in reversed(request.conversation.recent_messages)],
            *[self._extract_date_text(item) for item in reversed(request.conversation.long_term_memory)],
        ]
        reference_candidates = [
            request.appointment.booking_reference,
            self._extract_booking_reference(request.input),
            *[self._extract_booking_reference(item) for item in reversed(request.conversation.recent_messages)],
            *[self._extract_booking_reference(item) for item in reversed(request.conversation.long_term_memory)],
        ]
        appointment_date = next(
            (value for value in (self._normalize_date_text(item) for item in date_candidates) if value),
            None,
        )
        booking_reference = next(
            (value for value in (self._normalize_booking_reference(item) for item in reference_candidates) if value),
            None,
        )
        return appointment_date, booking_reference

    def _extract_date_text(self, text: str | None) -> str | None:
        if not text:
            return None
        match = DATE_TOKEN_RE.search(text)
        return match.group(1) if match else None

    def _extract_booking_reference(self, text: str | None) -> str | None:
        if not text:
            return None
        match = BOOKING_REFERENCE_RE.search(text)
        return match.group(0) if match else None

    def _normalize_date_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        match = DATE_TOKEN_RE.search(stripped)
        if match:
            return match.group(1)
        return stripped

    def _normalize_booking_reference(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        match = BOOKING_REFERENCE_RE.search(stripped)
        return match.group(0) if match else None
