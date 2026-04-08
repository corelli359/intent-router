from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ConfigDict, Field

from intent_agents.common import (
    AgentConversationContext,
    AgentCustomer,
    AgentExecutionResponse,
    AgentIntentContext,
    JsonObjectRunner,
    dump_json,
)
from models.intent import IntentSlotDefinition, SlotValueType
from router_core.slot_grounding import slot_value_grounded


class ForexAccount(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    card_number: str | int | None = Field(default=None, alias="cardNumber")
    phone_last4: str | int | None = Field(default=None, alias="phoneLast4")


class ForexExchangeDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_currency: str | None = Field(default=None, alias="sourceCurrency")
    target_currency: str | None = Field(default=None, alias="targetCurrency")
    amount: str | int | float | None = Field(default=None, alias="amount")


class ForexExchangeAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    intent: AgentIntentContext = Field(default_factory=AgentIntentContext)
    account: ForexAccount = Field(default_factory=ForexAccount)
    exchange: ForexExchangeDetails = Field(default_factory=ForexExchangeDetails)


class ForexExchangeResolution(BaseModel):
    card_number: str | None = None
    phone_last4: str | None = None
    source_currency: str | None = None
    target_currency: str | None = None
    amount: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供扣款卡号、手机号后4位、卖出币种、买入币种、换汇金额"


_CARD_SLOT = IntentSlotDefinition(slot_key="card_number", value_type=SlotValueType.ACCOUNT_NUMBER)
_PHONE_SLOT = IntentSlotDefinition(slot_key="phone_last4", value_type=SlotValueType.PHONE_LAST4)
_SOURCE_CURRENCY_SLOT = IntentSlotDefinition(slot_key="source_currency", value_type=SlotValueType.STRING)
_TARGET_CURRENCY_SLOT = IntentSlotDefinition(slot_key="target_currency", value_type=SlotValueType.STRING)
_AMOUNT_SLOT = IntentSlotDefinition(slot_key="amount", value_type=SlotValueType.CURRENCY)

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)")
_CURRENCY_ALIASES = {
    "CNY": ("人民币", "人名币", "cny", "rmb"),
    "USD": ("美元", "美金", "usd", "us dollar"),
    "EUR": ("欧元", "eur", "euro"),
    "JPY": ("日元", "jpy", "yen"),
    "HKD": ("港币", "港元", "hkd"),
    "GBP": ("英镑", "gbp"),
}
_RATE_TABLE = {
    ("CNY", "USD"): Decimal("0.14"),
    ("USD", "CNY"): Decimal("7.10"),
    ("CNY", "EUR"): Decimal("0.13"),
    ("EUR", "CNY"): Decimal("7.80"),
    ("CNY", "JPY"): Decimal("21.00"),
    ("JPY", "CNY"): Decimal("0.048"),
    ("USD", "HKD"): Decimal("7.80"),
    ("HKD", "USD"): Decimal("0.128"),
}

FOREX_EXCHANGE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是换外汇场景里的要素填充器。"
                "上游已经把请求路由到了 exchange_forex。"
                "你不做意图识别，不执行换汇，只做槽位提取和下一步追问。"
                "你必须只输出 JSON，不能输出解释。"
                "当前任务只关心五个槽位：card_number、phone_last4、source_currency、target_currency、amount。"
                "规则："
                "1. 优先保留 current_slots 里已经确认的值。"
                "2. recent_messages 和 long_term_memory 只能帮助你理解用户是不是在补充当前任务，"
                "不能把历史里出现过但当前轮没有再次明确提供的新槽位直接当成当前确认输入。"
                "3. currency 请统一输出 ISO 代码，例如人民币=CNY、美元=USD、欧元=EUR。"
                "4. 如果信息不足，has_enough_information 必须为 false，并给出明确 ask_message。"
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
                '  "card_number": "string | null",\n'
                '  "phone_last4": "string | null",\n'
                '  "source_currency": "string | null",\n'
                '  "target_currency": "string | null",\n'
                '  "amount": "string | null",\n'
                '  "has_enough_information": true,\n'
                '  "ask_message": "string"\n'
                "}}"
            ),
        ),
    ]
)


class ForexExchangeAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: ForexExchangeAgentRequest) -> AgentExecutionResponse:
        seeded = ForexExchangeResolution(
            card_number=self._normalize_card_number(request.account.card_number),
            phone_last4=self._normalize_phone_last4(request.account.phone_last4),
            source_currency=self._normalize_currency(request.exchange.source_currency),
            target_currency=self._normalize_currency(request.exchange.target_currency),
            amount=self._normalize_amount(request.exchange.amount),
        )
        resolution = await self._resolve(request, seeded)
        slot_memory = self._slot_memory(resolution)
        missing_fields = self._missing_fields(resolution)

        if missing_fields:
            return AgentExecutionResponse.waiting(
                resolution.ask_message or self._ask_message(missing_fields),
                slot_memory=slot_memory,
                payload={"agent": "exchange_forex", "missing_fields": missing_fields},
            )

        source_currency = resolution.source_currency or "CNY"
        target_currency = resolution.target_currency or "USD"
        amount_value = self._amount_value(resolution.amount) or Decimal("0")
        exchanged_amount = self._convert(amount_value, source_currency, target_currency)
        return AgentExecutionResponse.completed(
            f"已完成换汇：{resolution.amount} {source_currency} 兑换为 {exchanged_amount} {target_currency}",
            slot_memory=slot_memory,
            payload={
                "agent": "exchange_forex",
                "card_number": resolution.card_number,
                "phone_last_four": resolution.phone_last4,
                "source_currency": source_currency,
                "target_currency": target_currency,
                "sell_currency": source_currency,
                "buy_currency": target_currency,
                "amount": resolution.amount,
                "exchanged_amount": exchanged_amount,
                "converted_amount": exchanged_amount,
                "business_status": "success",
            },
        )

    async def _resolve(
        self,
        request: ForexExchangeAgentRequest,
        seeded: ForexExchangeResolution,
    ) -> ForexExchangeResolution:
        heuristic = self._extract_from_input(request.input)
        if self.resolver is None:
            return self._finalize_resolution(
                seeded,
                ForexExchangeResolution(
                    card_number=heuristic.card_number if not seeded.card_number else None,
                    phone_last4=heuristic.phone_last4 if not seeded.phone_last4 else None,
                    source_currency=heuristic.source_currency if not seeded.source_currency else None,
                    target_currency=heuristic.target_currency if not seeded.target_currency else None,
                    amount=heuristic.amount if not seeded.amount else None,
                ),
            )

        try:
            raw_payload = await self.resolver.run_json(
                prompt=FOREX_EXCHANGE_PROMPT,
                variables={
                    "intent_json": dump_json(request.intent.model_dump()),
                    "input_text": request.input,
                    "current_slots_json": dump_json(
                        {
                            "card_number": request.account.card_number,
                            "phone_last4": request.account.phone_last4,
                            "source_currency": request.exchange.source_currency,
                            "target_currency": request.exchange.target_currency,
                            "amount": request.exchange.amount,
                        }
                    ),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                    "long_term_memory_json": dump_json(request.conversation.long_term_memory),
                },
                schema=ForexExchangeResolution,
            )
            resolved = ForexExchangeResolution.model_validate(raw_payload)
        except Exception:
            resolved = ForexExchangeResolution()

        self._drop_unconfirmed_history_values(request, seeded, resolved)
        if heuristic.card_number and not resolved.card_number:
            resolved.card_number = heuristic.card_number
        if heuristic.phone_last4 and not resolved.phone_last4:
            resolved.phone_last4 = heuristic.phone_last4
        if heuristic.source_currency and not resolved.source_currency:
            resolved.source_currency = heuristic.source_currency
        if heuristic.target_currency and not resolved.target_currency:
            resolved.target_currency = heuristic.target_currency
        if heuristic.amount and not resolved.amount:
            resolved.amount = heuristic.amount
        return self._finalize_resolution(seeded, resolved)

    def _drop_unconfirmed_history_values(
        self,
        request: ForexExchangeAgentRequest,
        seeded: ForexExchangeResolution,
        resolved: ForexExchangeResolution,
    ) -> None:
        checks = [
            ("card_number", _CARD_SLOT, seeded.card_number),
            ("phone_last4", _PHONE_SLOT, seeded.phone_last4),
            ("source_currency", _SOURCE_CURRENCY_SLOT, seeded.source_currency),
            ("target_currency", _TARGET_CURRENCY_SLOT, seeded.target_currency),
            ("amount", _AMOUNT_SLOT, seeded.amount),
        ]
        for field_name, slot_def, seeded_value in checks:
            value = getattr(resolved, field_name)
            if value and value != seeded_value:
                if not slot_value_grounded(slot_def=slot_def, value=value, grounding_text=request.input):
                    setattr(resolved, field_name, None)

    def _finalize_resolution(
        self,
        seeded: ForexExchangeResolution,
        resolved: ForexExchangeResolution,
    ) -> ForexExchangeResolution:
        card_number = self._normalize_card_number(resolved.card_number) or seeded.card_number
        phone_last4 = self._normalize_phone_last4(resolved.phone_last4) or seeded.phone_last4
        source_currency = self._normalize_currency(resolved.source_currency) or seeded.source_currency
        target_currency = self._normalize_currency(resolved.target_currency) or seeded.target_currency
        amount = self._normalize_amount(resolved.amount) or seeded.amount
        completed = bool(source_currency and target_currency and amount)
        missing_fields = self._missing_fields(
            ForexExchangeResolution(
                card_number=card_number,
                phone_last4=phone_last4,
                source_currency=source_currency,
                target_currency=target_currency,
                amount=amount,
            )
        )
        return ForexExchangeResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            source_currency=source_currency,
            target_currency=target_currency,
            amount=amount,
            has_enough_information=completed,
            ask_message="" if completed else (resolved.ask_message or self._ask_message(missing_fields)),
        )

    def _extract_from_input(self, text: str) -> ForexExchangeResolution:
        card_number = self._extract_card_number(text)
        phone_last4 = self._extract_phone_last4(text)
        source_currency, target_currency = self._extract_currency_pair(text)
        amount = self._extract_amount(text, card_number, phone_last4)
        return ForexExchangeResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            source_currency=source_currency,
            target_currency=target_currency,
            amount=amount,
        )

    def _extract_card_number(self, text: str) -> str | None:
        digit_runs = self._digit_runs(text)
        return next((run for run in digit_runs if 12 <= len(run) <= 19), None)

    def _extract_phone_last4(self, text: str) -> str | None:
        explicit_markers = ("尾号", "后4位", "后四位", "手机", "手机号")
        for marker in explicit_markers:
            marker_index = text.find(marker)
            if marker_index < 0:
                continue
            trailing_runs = self._digit_runs(text[marker_index + len(marker):])
            if trailing_runs:
                return trailing_runs[0][-4:]
        return None

    def _extract_currency_pair(self, text: str) -> tuple[str | None, str | None]:
        matches: list[tuple[int, str]] = []
        lowered = text.lower()
        for code, aliases in _CURRENCY_ALIASES.items():
            for alias in aliases:
                position = lowered.find(alias.lower())
                if position >= 0:
                    matches.append((position, code))
                    break
        matches.sort(key=lambda item: item[0])
        ordered_codes: list[str] = []
        for _, code in matches:
            if code not in ordered_codes:
                ordered_codes.append(code)
        if len(ordered_codes) >= 2:
            return ordered_codes[0], ordered_codes[1]
        return None, None

    def _extract_amount(self, text: str, card_number: str | None, phone_last4: str | None) -> str | None:
        matches = _AMOUNT_RE.findall(text)
        for match in matches:
            normalized = self._normalize_amount(match)
            if normalized is None:
                continue
            if normalized in {card_number, phone_last4}:
                continue
            return normalized
        return None

    def _normalize_card_number(self, value: str | int | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        return digits or None

    def _normalize_phone_last4(self, value: str | int | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        return digits[-4:] if len(digits) >= 4 else None

    def _normalize_currency(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().upper()
        if normalized in _CURRENCY_ALIASES:
            return normalized
        lowered = str(value).strip().lower()
        for code, aliases in _CURRENCY_ALIASES.items():
            if lowered in {alias.lower() for alias in aliases}:
                return code
        return None

    def _normalize_amount(self, value: str | int | float | None) -> str | None:
        if value is None:
            return None
        digits: list[str] = []
        dot_seen = False
        for character in str(value):
            if character.isdigit():
                digits.append(character)
                continue
            if character == "." and not dot_seen:
                digits.append(character)
                dot_seen = True
        normalized = "".join(digits).strip(".")
        if not normalized:
            return None
        try:
            amount = Decimal(normalized)
        except InvalidOperation:
            return None
        if amount == amount.to_integral():
            return format(amount.quantize(Decimal("1")), "f")
        return format(amount.normalize(), "f")

    def _amount_value(self, value: str | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(value)
        except InvalidOperation:
            return None

    def _convert(self, amount: Decimal, source_currency: str, target_currency: str) -> str:
        if source_currency == target_currency:
            converted = amount
        else:
            rate = _RATE_TABLE.get((source_currency, target_currency), Decimal("1"))
            converted = amount * rate
        return format(converted.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    def _missing_fields(self, resolution: ForexExchangeResolution) -> list[str]:
        missing: list[str] = []
        if not resolution.source_currency:
            missing.append("source_currency")
        if not resolution.target_currency:
            missing.append("target_currency")
        if not resolution.amount:
            missing.append("amount")
        return missing

    def _ask_message(self, missing_fields: list[str]) -> str:
        labels = {
            "source_currency": "卖出币种",
            "target_currency": "买入币种",
            "amount": "换汇金额",
        }
        return "请提供" + "、".join(labels[field] for field in missing_fields)

    def _slot_memory(self, resolution: ForexExchangeResolution) -> dict[str, Any]:
        slot_memory: dict[str, Any] = {}
        if resolution.card_number:
            slot_memory["card_number"] = resolution.card_number
        if resolution.phone_last4:
            slot_memory["phone_last_four"] = resolution.phone_last4
        if resolution.source_currency:
            slot_memory["source_currency"] = resolution.source_currency
        if resolution.target_currency:
            slot_memory["target_currency"] = resolution.target_currency
        if resolution.amount:
            slot_memory["amount"] = resolution.amount
        return slot_memory

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
