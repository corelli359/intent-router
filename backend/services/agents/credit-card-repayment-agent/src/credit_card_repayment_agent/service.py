from __future__ import annotations

from decimal import Decimal
from typing import Any
from collections.abc import AsyncIterator

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from .support import (
    AgentExecutionResponse,
    AgentStreamEvent,
    ConfigVariablesRequest,
    JsonObjectRunner,
    dump_json,
)


class CreditCardRepaymentAgentRequest(ConfigVariablesRequest):
    pass


class CreditCardRepaymentResolution(BaseModel):
    card_number: str | None = None
    phone_last4: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供信用卡卡号和手机号后4位"


CREDIT_CARD_REPAYMENT_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是信用卡还款信息查询场景里的要素填充器。"
                "上游已经把请求路由到了 query_credit_card_repayment。"
                "你不做意图识别，不执行还款，只做槽位提取。"
                "你必须只输出 JSON，不能输出解释。"
                "当前任务只关心两个槽位：card_number 和 phone_last4。"
                "优先保留 current_slots 里已经确认的值；当前输入只补缺失信息，不要猜测。"
                "如果信息不足，has_enough_information 必须为 false，并给出简洁 ask_message。"
            ),
        ),
        (
            "human",
            (
                "intent(JSON):\n{intent_json}\n\n"
                "current_input:\n{input_text}\n\n"
                "current_slots(JSON):\n{credit_card_json}\n\n"
                "recent_messages(JSON):\n{recent_messages_json}\n\n"
                "long_term_memory(JSON):\n{long_term_memory_json}\n\n"
                "请返回 JSON:\n"
                "{{\n"
                '  "card_number": "string | null",\n'
                '  "phone_last4": "string | null",\n'
                '  "has_enough_information": true,\n'
                '  "ask_message": "string"\n'
                "}}"
            ),
        ),
    ]
)


class CreditCardRepaymentAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: CreditCardRepaymentAgentRequest) -> AgentExecutionResponse:
        slots = request.get_slots_data()
        seeded = CreditCardRepaymentResolution(
            card_number=self._normalize_card_number(slots.get("card_number")),
            phone_last4=self._normalize_phone_last4(slots.get("phone_last4")),
        )
        direct_resolution = self._finalize_resolution(seeded, CreditCardRepaymentResolution())
        if not request.txt.strip() and direct_resolution.has_enough_information:
            resolution = direct_resolution
        else:
            resolution = await self._resolve(request, seeded)
        slot_memory = self._slot_memory(resolution)

        if not resolution.has_enough_information:
            return AgentExecutionResponse.waiting(
                resolution.ask_message or self._ask_message(resolution.card_number, resolution.phone_last4),
                slot_memory=slot_memory,
                payload={
                    "agent": "query_credit_card_repayment",
                    "missing_fields": self._missing_fields(resolution.card_number, resolution.phone_last4),
                },
            )

        due_amount = Decimal("3200")
        minimum_payment = Decimal("320")
        due_date = "2026-04-25"
        return AgentExecutionResponse.completed(
            f"查询成功，本期信用卡应还 {due_amount} 元，最低还款 {minimum_payment} 元，到期日 {due_date}",
            slot_memory=slot_memory,
            payload={
                "agent": "query_credit_card_repayment",
                "card_number": resolution.card_number,
                "phone_last_four": resolution.phone_last4,
                "statement_amount": float(due_amount),
                "due_amount": float(due_amount),
                "minimum_due": float(minimum_payment),
                "minimum_payment": float(minimum_payment),
                "due_date": due_date,
                "business_status": "completed",
            },
        )

    async def handle_stream(self, request: CreditCardRepaymentAgentRequest) -> AsyncIterator[str]:
        """Handle the request and yield SSE formatted events."""
        response = await self.handle(request)

        output_payload = {
            "event": response.event,
            "content": response.content,
            "ishandover": response.ishandover,
            "status": response.status,
            "slot_memory": response.slot_memory,
            "payload": response.payload,
        }

        end_event = AgentStreamEvent.from_node_output(
            node_id="end",
            node_title="结束",
            output=output_payload,
        )
        yield end_event.to_sse(event="message")

        yield "event:done\ndata:[DONE]\n\n"

    async def _resolve(
        self,
        request: CreditCardRepaymentAgentRequest,
        seeded: CreditCardRepaymentResolution,
    ) -> CreditCardRepaymentResolution:
        heuristic = self._extract_from_input(request.txt)
        if self.resolver is None:
            return self._finalize_resolution(seeded, heuristic)

        try:
            slots = request.get_slots_data()
            raw_payload = await self.resolver.run_json(
                prompt=CREDIT_CARD_REPAYMENT_PROMPT,
                variables={
                    "intent_json": request.get_config_value("intent", "{}"),
                    "input_text": request.txt,
                    "credit_card_json": dump_json({
                        "card_number": slots.get("card_number"),
                        "phone_last4": slots.get("phone_last4"),
                    }),
                    "recent_messages_json": request.get_config_value("recent_messages", "[]"),
                    "long_term_memory_json": request.get_config_value("long_term_memory", "[]"),
                },
                schema=CreditCardRepaymentResolution,
            )
            resolved = CreditCardRepaymentResolution.model_validate(raw_payload)
        except Exception:
            resolved = CreditCardRepaymentResolution()

        if heuristic.card_number and not resolved.card_number:
            resolved.card_number = heuristic.card_number
        if heuristic.phone_last4 and not resolved.phone_last4:
            resolved.phone_last4 = heuristic.phone_last4
        return self._finalize_resolution(seeded, resolved)

    def _finalize_resolution(
        self,
        seeded: CreditCardRepaymentResolution,
        resolved: CreditCardRepaymentResolution,
    ) -> CreditCardRepaymentResolution:
        card_number = self._normalize_card_number(resolved.card_number) or seeded.card_number
        phone_last4 = self._normalize_phone_last4(resolved.phone_last4) or seeded.phone_last4
        has_enough_information = bool(card_number and phone_last4)
        ask_message = "" if has_enough_information else self._ask_message(card_number, phone_last4)
        return CreditCardRepaymentResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            has_enough_information=has_enough_information,
            ask_message=ask_message,
        )

    def _normalize_card_number(self, value: str | int | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        return digits or None

    def _normalize_phone_last4(self, value: str | int | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        if len(digits) >= 4:
            return digits[-4:]
        return None

    def _missing_fields(self, card_number: str | None, phone_last4: str | None) -> list[str]:
        missing: list[str] = []
        if not card_number:
            missing.append("card_number")
        if not phone_last4:
            missing.append("phone_last_four")
        return missing

    def _ask_message(self, card_number: str | None, phone_last4: str | None) -> str:
        missing = self._missing_fields(card_number, phone_last4)
        if missing == ["card_number", "phone_last_four"]:
            return "请提供信用卡卡号和手机号后4位"
        if missing == ["card_number"]:
            return "请提供信用卡卡号"
        if missing == ["phone_last_four"]:
            return "请提供手机号后4位"
        return "请提供信用卡卡号和手机号后4位"

    def _slot_memory(self, resolution: CreditCardRepaymentResolution) -> dict[str, Any]:
        slot_memory: dict[str, Any] = {}
        if resolution.card_number:
            slot_memory["card_number"] = resolution.card_number
        if resolution.phone_last4:
            slot_memory["phone_last_four"] = resolution.phone_last4
        return slot_memory

    def _extract_from_input(self, text: str) -> CreditCardRepaymentResolution:
        digit_runs = self._digit_runs(text)
        card_number = next((run for run in digit_runs if 12 <= len(run) <= 19), None)
        phone_last4 = next((run[-4:] for run in digit_runs if run != card_number and len(run) >= 4), None)
        return CreditCardRepaymentResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            has_enough_information=bool(card_number and phone_last4),
            ask_message="",
        )

    def _digit_runs(self, text: str) -> list[str]:
        runs: list[str] = []
        current: list[str] = []
        for character in text:
            if character.isdigit():
                current.append(character)
                continue
            if current:
                runs.append("".join(current))
                current = []
        if current:
            runs.append("".join(current))
        return runs
