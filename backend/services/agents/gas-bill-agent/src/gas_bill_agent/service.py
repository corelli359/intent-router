from __future__ import annotations

import json

import re
from decimal import Decimal
from typing import Any
from collections.abc import AsyncIterator

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from .support import (
    AgentExecutionResponse,
    ConfigVariablesRequest,
    JsonObjectRunner,
    dump_json,
    AgentStreamEvent,
)
from .finance_utils import amount_value, digit_runs, format_decimal, normalize_amount


class GasBillPaymentAgentRequest(ConfigVariablesRequest):
    pass


class GasBillPaymentResolution(BaseModel):
    gas_account_number: str | None = None
    amount: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供燃气户号和缴费金额"


GAS_BILL_PAYMENT_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是天然气缴费场景里的要素填充器。"
                "上游已经把请求路由到了 pay_gas_bill。"
                "你不做意图识别，不执行缴费，只做槽位提取。"
                "你必须只输出 JSON，不能输出解释。"
                "当前任务只关心两个槽位：gas_account_number 和 amount。"
                "优先保留 current_slots 中已确认的值。"
            ),
        ),
        (
            "human",
            (
                "intent(JSON):\n{intent_json}\n\n"
                "current_input:\n{input_text}\n\n"
                "current_slots(JSON):\n{current_slots_json}\n\n"
                "recent_messages(JSON):\n{recent_messages_json}\n\n"
                "long_term_memory(JSON):\n{long_term_memory_json}\n\n"
                "请返回 JSON:\n"
                "{{\n"
                '  "gas_account_number": "string | null",\n'
                '  "amount": "string | null",\n'
                '  "has_enough_information": true,\n'
                '  "ask_message": "string"\n'
                "}}"
            ),
        ),
    ]
)


class GasBillPaymentAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: GasBillPaymentAgentRequest) -> AgentExecutionResponse:
        slots = request.get_slots_data()
        seeded = GasBillPaymentResolution(
            gas_account_number=self._normalize_account_number(slots.get("gas_account_number")),
            amount=normalize_amount(slots.get("amount")),
        )
        direct_resolution = self._finalize_resolution(seeded, GasBillPaymentResolution())
        if not request.txt.strip() and direct_resolution.has_enough_information:
            resolution = direct_resolution
        else:
            resolution = await self._resolve(request, seeded)
        slot_memory = self._slot_memory(resolution)
        missing_fields = self._missing_fields(resolution)

        if missing_fields:
            return AgentExecutionResponse.waiting(
                resolution.ask_message or self._ask_message(missing_fields),
                slot_memory=slot_memory,
                payload={"agent": "pay_gas_bill", "missing_fields": missing_fields},
            )

        amount = amount_value(resolution.amount)
        if amount is not None and amount > Decimal("5000"):
            return AgentExecutionResponse.failed(
                "天然气缴费金额超出单笔限制",
                payload={"agent": "pay_gas_bill", "business_status": "limit_exceeded"},
            )

        return AgentExecutionResponse.completed(
            f"已为燃气户号 {resolution.gas_account_number} 缴费 {resolution.amount} 元",
            slot_memory=slot_memory,
            payload={
                "agent": "pay_gas_bill",
                "gas_account_number": resolution.gas_account_number,
                "amount": resolution.amount,
                "business_status": "completed",
            },
        )
    async def handle_stream(self, request: GasBillPaymentAgentRequest) -> AsyncIterator[str]:
        """Handle the request and yield SSE formatted events matching Router expectations."""
        response = await self.handle(request)

        output = {
            "event": response.event,
            "content": response.content,
            "ishandover": response.ishandover,
            "status": response.status,
            "slot_memory": response.slot_memory,
            "payload": response.payload,
        }

        yield f"event:message\ndata:{json.dumps(output, ensure_ascii=False)}\n\n"
        yield "event:done\ndata:[DONE]\n\n"

    async def _resolve(
        self,
        request: GasBillPaymentAgentRequest,
        seeded: GasBillPaymentResolution,
    ) -> GasBillPaymentResolution:
        heuristic = self._extract_from_input(request.txt)
        if self.resolver is None:
            return self._finalize_resolution(seeded, heuristic)

        try:
            slots = request.get_slots_data()
            raw_payload = await self.resolver.run_json(
                prompt=GAS_BILL_PAYMENT_PROMPT,
                variables={
                    "intent_json": request.get_config_value("intent", "{}"),
                    "input_text": request.txt,
                    "current_slots_json": dump_json({
                        "gas_account_number": slots.get("gas_account_number"),
                        "amount": slots.get("amount"),
                    }),
                    "recent_messages_json": request.get_config_value("recent_messages", "[]"),
                    "long_term_memory_json": request.get_config_value("long_term_memory", "[]"),
                },
                schema=GasBillPaymentResolution,
            )
            resolved = GasBillPaymentResolution.model_validate(raw_payload)
        except Exception:
            resolved = GasBillPaymentResolution()

        if heuristic.gas_account_number and not resolved.gas_account_number:
            resolved.gas_account_number = heuristic.gas_account_number
        if heuristic.amount and not resolved.amount:
            resolved.amount = heuristic.amount
        return self._finalize_resolution(seeded, resolved)

    def _finalize_resolution(
        self,
        seeded: GasBillPaymentResolution,
        resolved: GasBillPaymentResolution,
    ) -> GasBillPaymentResolution:
        gas_account_number = self._normalize_account_number(resolved.gas_account_number) or seeded.gas_account_number
        amount = normalize_amount(resolved.amount) or seeded.amount
        has_enough_information = bool(gas_account_number and amount)
        ask_message = "" if has_enough_information else self._ask_message(
            self._missing_fields(
                GasBillPaymentResolution(
                    gas_account_number=gas_account_number,
                    amount=amount,
                )
            )
        )
        return GasBillPaymentResolution(
            gas_account_number=gas_account_number,
            amount=amount,
            has_enough_information=has_enough_information,
            ask_message=ask_message,
        )

    def _extract_from_input(self, text: str) -> GasBillPaymentResolution:
        runs = digit_runs(text)
        explicit_account = re.search(r"(?:户号|账户|燃气号|编号)\D*(\d{6,20})", text)
        account_number = explicit_account.group(1) if explicit_account else None
        amount = normalize_amount(self._extract_amount_text(text))
        if account_number is None:
            account_candidates = [
                run
                for run in runs
                if len(run) >= 6 and run != (amount.replace(".", "") if amount else "")
            ]
            account_number = account_candidates[0] if account_candidates else None
        return GasBillPaymentResolution(
            gas_account_number=account_number,
            amount=amount,
            has_enough_information=bool(account_number and amount),
            ask_message="",
        )

    def _extract_amount_text(self, text: str) -> str | None:
        explicit = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块)", text)
        if explicit:
            return explicit.group(1)
        prefixed = re.search(r"(?:金额|缴费|交费|充值)\D*(\d+(?:\.\d+)?)", text)
        if prefixed:
            return prefixed.group(1)
        return None

    def _normalize_account_number(self, value: str | int | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        return digits or None

    def _missing_fields(self, resolution: GasBillPaymentResolution) -> list[str]:
        missing: list[str] = []
        if not resolution.gas_account_number:
            missing.append("gas_account_number")
        if not resolution.amount:
            missing.append("amount")
        return missing

    def _ask_message(self, missing_fields: list[str]) -> str:
        if missing_fields == ["gas_account_number", "amount"]:
            return "请提供燃气户号和缴费金额"
        if missing_fields == ["gas_account_number"]:
            return "请提供燃气户号"
        if missing_fields == ["amount"]:
            return "请提供缴费金额"
        return "请提供燃气户号和缴费金额"

    def _slot_memory(self, resolution: GasBillPaymentResolution) -> dict[str, Any]:
        slot_memory: dict[str, Any] = {}
        if resolution.gas_account_number:
            slot_memory["gas_account_number"] = resolution.gas_account_number
        if resolution.amount:
            slot_memory["amount"] = resolution.amount
        return slot_memory
