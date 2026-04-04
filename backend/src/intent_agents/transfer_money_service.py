from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
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


CARD_NUMBER_RE = re.compile(r"\b(\d{12,19})\b")
PHONE_LAST4_RE = re.compile(r"(?:后4位|后四位|尾号)\D*(\d{4})")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
AMOUNT_LABEL_RE = re.compile(r"(?:转账金额|金额)\D*(\d+(?:\.\d+)?)")
NAME_RE = re.compile(
    r"(?:给|向|转给|转账给)([\u4e00-\u9fffA-Za-z]{2,16}?)(?=(?:转账|转|汇款|付款|支付|卡号|银行卡|手机号|尾号|后4位|后四位|金额|[，,。\s]|$))"
)
INVALID_NAME_TOKENS = ("帮我", "转账", "收款人", "卡号", "手机号", "金额", "尾号")


class TransferRecipient(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, alias="name")
    card_number: str | None = Field(default=None, alias="cardNumber")
    phone_last4: str | None = Field(default=None, alias="phoneLast4")


class TransferDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    amount: str | None = Field(default=None, alias="amount")


class TransferMoneyAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    recipient: TransferRecipient = Field(default_factory=TransferRecipient)
    transfer: TransferDetails = Field(default_factory=TransferDetails)


class TransferMoneyResolution(BaseModel):
    recipient_name: str | None = None
    recipient_card_number: str | None = None
    recipient_phone_last4: str | None = None
    amount: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供收款人姓名、收款卡号、收款人手机号后4位、转账金额"


class TransferMoneyAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: TransferMoneyAgentRequest) -> AgentExecutionResponse:
        resolution = await self._resolve(request)
        slot_memory = self._slot_memory(resolution)
        missing_fields = self._missing_fields(resolution)
        payload = {
            "agent": "transfer_money",
            "recipient_name": resolution.recipient_name,
            "recipient_card_number": resolution.recipient_card_number,
            "recipient_phone_last_four": resolution.recipient_phone_last4,
            "amount": resolution.amount,
        }

        if missing_fields:
            return AgentExecutionResponse.waiting(
                self._ask_message(missing_fields),
                slot_memory=slot_memory,
                payload={**payload, "missing_fields": missing_fields},
            )

        amount_value = self._amount_value(resolution.amount)
        if amount_value is not None and amount_value > Decimal("8000"):
            return AgentExecutionResponse(
                event="final",
                content="账户余额不足",
                ishandover=True,
                status="failed",
                slot_memory=slot_memory,
                payload={**payload, "business_status": "insufficient_balance", "balance": 8000},
            )

        amount_text = resolution.amount or "0"
        return AgentExecutionResponse.completed(
            f"已向{resolution.recipient_name}转账 {amount_text} 元，转账成功",
            slot_memory=slot_memory,
            payload={**payload, "business_status": "success"},
        )

    async def _resolve(self, request: TransferMoneyAgentRequest) -> TransferMoneyResolution:
        seeded = TransferMoneyResolution(
            recipient_name=self._normalize_name(request.recipient.name),
            recipient_card_number=self._normalize_card_number(request.recipient.card_number),
            recipient_phone_last4=self._normalize_phone_last4(request.recipient.phone_last4),
            amount=self._normalize_amount(request.transfer.amount),
        )

        if self.resolver is None:
            return self._heuristic_resolution(request, seeded)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "你是转账意图 agent 里的参数解析器。"
                        "上游已经把请求路由到了 transfer_money，所以你不做意图判断，也不执行转账。"
                        "你只能输出 JSON，不能输出解释。"
                        "这个 agent 是示意实现，只需要抽取四个字段："
                        "recipient_name、recipient_card_number、recipient_phone_last4、amount。"
                        "优先保留当前已知槽位，再从当前输入和最近对话里补齐。"
                        "用语义理解，不要死抠字面。下面这些模式只是识别提示，不是让你机械匹配："
                        "收款卡号通常是 12 到 19 位数字串；"
                        "手机号后四位通常表现为 后4位/后四位/尾号 加 4 位数字；"
                        "金额通常表现为 数字 加 元。"
                        "严格约束："
                        "1. “帮我给李四转 5000 元” 只应提取 recipient_name=李四, amount=5000，其余字段保持 null。"
                        "2. 不要把 5000 当成手机号后四位。"
                        "3. 不要从无关历史里猜收款卡号或收款人手机号。"
                        "4. 如果上一轮已经只缺金额，那么用户回复 “5000”、“5000元”、“5000 元”、“金额5000”、“转账金额 5000” 都应识别为 amount=5000。"
                        "5. 如果信息不全，has_enough_information 必须为 false。"
                    ),
                ),
                (
                    "human",
                    (
                        "当前输入:\n{input_text}\n\n"
                        "当前已知收款人信息(JSON):\n{recipient_json}\n\n"
                        "当前已知转账信息(JSON):\n{transfer_json}\n\n"
                        "最近对话(JSON):\n{recent_messages_json}\n\n"
                        "请返回 JSON:\n"
                        "{\n"
                        '  "recipient_name": "string | null",\n'
                        '  "recipient_card_number": "string | null",\n'
                        '  "recipient_phone_last4": "string | null",\n'
                        '  "amount": "string | null",\n'
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
                    "recipient_json": dump_json(request.recipient.model_dump()),
                    "transfer_json": dump_json(request.transfer.model_dump()),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                },
            )
            resolution = TransferMoneyResolution.model_validate(raw_payload)
        except Exception:
            return self._heuristic_resolution(request, seeded)

        resolution.recipient_name = seeded.recipient_name or self._normalize_name(resolution.recipient_name)
        resolution.recipient_card_number = seeded.recipient_card_number or self._normalize_card_number(
            resolution.recipient_card_number
        )
        resolution.recipient_phone_last4 = seeded.recipient_phone_last4 or self._normalize_phone_last4(
            resolution.recipient_phone_last4
        )
        resolution.amount = seeded.amount or self._normalize_amount(resolution.amount)
        resolution.has_enough_information = not self._missing_fields(resolution)
        if not resolution.has_enough_information:
            resolution.ask_message = self._ask_message(self._missing_fields(resolution))
        return resolution

    def _heuristic_resolution(
        self,
        request: TransferMoneyAgentRequest,
        seeded: TransferMoneyResolution,
    ) -> TransferMoneyResolution:
        recipient_name = seeded.recipient_name or self._extract_recipient_name(request.input)
        recipient_card_number = seeded.recipient_card_number or self._extract_card_number(request.input)
        recipient_phone_last4 = seeded.recipient_phone_last4 or self._extract_phone_last4(
            request.input,
            allow_standalone=bool(seeded.amount),
        )
        amount = seeded.amount or self._extract_amount(
            request.input,
            allow_standalone=bool(seeded.recipient_name or seeded.recipient_card_number or seeded.recipient_phone_last4),
        )
        resolution = TransferMoneyResolution(
            recipient_name=recipient_name,
            recipient_card_number=recipient_card_number,
            recipient_phone_last4=recipient_phone_last4,
            amount=amount,
        )
        resolution.has_enough_information = not self._missing_fields(resolution)
        resolution.ask_message = self._ask_message(self._missing_fields(resolution))
        return resolution

    def _extract_recipient_name(self, text: str) -> str | None:
        match = NAME_RE.search(text)
        if match:
            return self._normalize_name(match.group(1))
        stripped = text.strip()
        if any(token in stripped for token in INVALID_NAME_TOKENS):
            return None
        return self._normalize_name(stripped)

    def _extract_card_number(self, text: str) -> str | None:
        match = CARD_NUMBER_RE.search(text)
        if match:
            return self._normalize_card_number(match.group(1))
        stripped = text.strip()
        return self._normalize_card_number(stripped)

    def _extract_phone_last4(self, text: str, *, allow_standalone: bool) -> str | None:
        match = PHONE_LAST4_RE.search(text)
        if match:
            return self._normalize_phone_last4(match.group(1))
        if allow_standalone and text.strip().isdigit() and len(text.strip()) == 4:
            return self._normalize_phone_last4(text.strip())
        return None

    def _extract_amount(self, text: str, *, allow_standalone: bool) -> str | None:
        match = AMOUNT_RE.search(text)
        if match:
            return self._normalize_amount(match.group(1))
        label_match = AMOUNT_LABEL_RE.search(text)
        if label_match:
            return self._normalize_amount(label_match.group(1))
        if allow_standalone and text.strip().isdigit():
            return self._normalize_amount(text.strip())
        return None

    def _normalize_name(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,16}", stripped):
            return stripped
        return None

    def _normalize_card_number(self, value: str | None) -> str | None:
        if value is None:
            return None
        digits = re.sub(r"\D", "", value)
        if 12 <= len(digits) <= 19:
            return digits
        return None

    def _normalize_phone_last4(self, value: str | None) -> str | None:
        if value is None:
            return None
        digits = re.sub(r"\D", "", value)
        if len(digits) == 11:
            return digits[-4:]
        if len(digits) == 4:
            return digits
        return None

    def _normalize_amount(self, value: str | None) -> str | None:
        if value is None:
            return None
        digits = re.sub(r"[^\d.]", "", str(value))
        if not digits:
            return None
        try:
            amount = Decimal(digits)
        except InvalidOperation:
            return None
        normalized = amount.quantize(Decimal("1")) if amount == amount.to_integral() else amount.normalize()
        return format(normalized, "f")

    def _amount_value(self, value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            return None

    def _missing_fields(self, resolution: TransferMoneyResolution) -> list[str]:
        missing: list[str] = []
        if not resolution.recipient_name:
            missing.append("recipient_name")
        if not resolution.recipient_card_number:
            missing.append("recipient_card_number")
        if not resolution.recipient_phone_last4:
            missing.append("recipient_phone_last_four")
        if not resolution.amount:
            missing.append("amount")
        return missing

    def _ask_message(self, missing_fields: list[str]) -> str:
        labels = {
            "recipient_name": "收款人姓名",
            "recipient_card_number": "收款卡号",
            "recipient_phone_last_four": "收款人手机号后4位",
            "amount": "转账金额",
        }
        if not missing_fields:
            return ""
        return "请提供" + "、".join(labels[field] for field in missing_fields)

    def _slot_memory(self, resolution: TransferMoneyResolution) -> dict[str, Any]:
        slot_memory: dict[str, Any] = {}
        if resolution.recipient_name:
            slot_memory["recipient_name"] = resolution.recipient_name
        if resolution.recipient_card_number:
            slot_memory["recipient_card_number"] = resolution.recipient_card_number
        if resolution.recipient_phone_last4:
            slot_memory["recipient_phone_last_four"] = resolution.recipient_phone_last4
        if resolution.amount:
            slot_memory["amount"] = resolution.amount
        return slot_memory
