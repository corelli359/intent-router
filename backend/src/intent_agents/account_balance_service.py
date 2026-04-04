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


CARD_NUMBER_RE = re.compile(r"\b(\d{12,19})\b")
EXPLICIT_BALANCE_CARD_RE = re.compile(
    r"(?:(?:我的|本人|本人的)\s*)?(?<!收款)(?<!对方)(?<!目标)(?:银行卡号|银行卡|卡号)\D*([0-9][0-9\s-]{10,30}[0-9])"
)
EXPLICIT_BALANCE_PHONE_RE = re.compile(
    r"(?:(?:我的|本人|本人的)\s*)?(?<!收款)(?<!对方)(?<!目标)(?:手机号?(?:后4位|后四位|末4位|末四位|尾号)|后4位|后四位|尾号)\D*(\d{4})"
)
PHONE_NUMBER_RE = re.compile(r"\b1\d{10}\b")
FOUR_DIGITS_ONLY_RE = re.compile(r"^\D*(\d{4})\D*$")
BALANCE_INTENT_MEMORY_PREFIXES = ("query_account_balance", "order_status")


class BalanceAccount(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    card_number: str | None = Field(default=None, alias="cardNumber")
    phone_last4: str | None = Field(default=None, alias="phoneLast4")


class AccountBalanceAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    account: BalanceAccount = Field(default_factory=BalanceAccount)


class AccountBalanceResolution(BaseModel):
    card_number: str | None = None
    phone_last4: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供卡号和手机号后4位"


class AccountBalanceAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: AccountBalanceAgentRequest) -> AgentExecutionResponse:
        resolution = await self._resolve(request)
        slot_memory: dict[str, Any] = {}
        if resolution.card_number:
            slot_memory["card_number"] = resolution.card_number
        if resolution.phone_last4:
            slot_memory["phone_last_four"] = resolution.phone_last4

        if resolution.has_enough_information:
            return AgentExecutionResponse.completed(
                "查询成功，账户余额为 8000 元",
                slot_memory=slot_memory,
                payload={
                    "agent": "query_account_balance",
                    "balance": 8000,
                    "card_number": resolution.card_number,
                    "phone_last_four": resolution.phone_last4,
                    "business_status": "completed",
                },
            )

        return AgentExecutionResponse.waiting(
            self._ask_message(resolution.card_number, resolution.phone_last4),
            slot_memory=slot_memory,
            payload={
                "agent": "query_account_balance",
                "missing_fields": self._missing_fields(resolution.card_number, resolution.phone_last4),
            },
        )

    async def _resolve(self, request: AccountBalanceAgentRequest) -> AccountBalanceResolution:
        seeded_card_number, seeded_phone_last4 = self._seed_context(request)
        if self.resolver is None:
            return self._heuristic_resolution(seeded_card_number, seeded_phone_last4)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "你是账户余额查询服务里的参数解析器。"
                        "不要直接回答用户问题，只输出 JSON。"
                        "目标是判断是否已经拿到查询余额所需的 card_number 和 phone_last4。"
                        "如果信息不够，不要猜测。"
                    ),
                ),
                (
                    "human",
                    (
                        "当前输入:\n{input_text}\n\n"
                        "当前已知账户信息(JSON):\n{account_json}\n\n"
                        "最近对话(JSON):\n{recent_messages_json}\n\n"
                        "长期记忆(JSON):\n{long_term_memory_json}\n\n"
                        "请返回 JSON:\n"
                        "{\n"
                        '  "card_number": "string | null",\n'
                        '  "phone_last4": "string | null",\n'
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
                    "account_json": dump_json(request.account.model_dump()),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                    "long_term_memory_json": dump_json(request.conversation.long_term_memory),
                },
            )
            resolution = AccountBalanceResolution.model_validate(raw_payload)
        except Exception:
            return self._heuristic_resolution(seeded_card_number, seeded_phone_last4)

        if not resolution.card_number and seeded_card_number:
            resolution.card_number = seeded_card_number
        if not resolution.phone_last4 and seeded_phone_last4:
            resolution.phone_last4 = seeded_phone_last4
        resolution.card_number = self._normalize_card_number(resolution.card_number)
        resolution.phone_last4 = self._normalize_phone_last4(resolution.phone_last4)
        resolution.has_enough_information = bool(resolution.card_number and resolution.phone_last4)
        if not resolution.has_enough_information and not resolution.ask_message.strip():
            resolution.ask_message = self._ask_message(resolution.card_number, resolution.phone_last4)
        return resolution

    def _heuristic_resolution(
        self,
        card_number: str | None,
        phone_last4: str | None,
    ) -> AccountBalanceResolution:
        return AccountBalanceResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            has_enough_information=bool(card_number and phone_last4),
            ask_message=self._ask_message(card_number, phone_last4),
        )

    def _seed_context(self, request: AccountBalanceAgentRequest) -> tuple[str | None, str | None]:
        user_messages = self._filter_user_messages(request.conversation.recent_messages)
        balance_memory_entries = self._balance_intent_memory_entries(request.conversation.long_term_memory)
        card_candidates = [
            request.account.card_number,
            self._extract_balance_card_number(request.input),
            *[self._extract_balance_card_number(item) for item in reversed(user_messages)],
            *[self._extract_balance_card_number(item) for item in reversed(balance_memory_entries)],
        ]
        phone_candidates = [
            request.account.phone_last4,
            self._extract_balance_phone_last4(request.input),
            *[self._extract_balance_phone_last4(item) for item in reversed(user_messages)],
            *[self._extract_balance_phone_last4(item) for item in reversed(balance_memory_entries)],
        ]
        card_number = next(
            (value for value in (self._normalize_card_number(item) for item in card_candidates) if value),
            None,
        )
        phone_last4 = next(
            (value for value in (self._normalize_phone_last4(item) for item in phone_candidates) if value),
            None,
        )
        return card_number, phone_last4

    def _filter_user_messages(self, entries: list[str]) -> list[str]:
        return [entry for entry in entries if self._is_user_message(entry)]

    def _is_user_message(self, text: str | None) -> bool:
        if not text:
            return False
        return text.strip().lower().startswith("user:")

    def _balance_intent_memory_entries(self, entries: list[str]) -> list[str]:
        return [entry for entry in entries if self._is_balance_intent_memory_entry(entry)]

    def _is_balance_intent_memory_entry(self, text: str | None) -> bool:
        if not text:
            return False
        normalized = text.strip().lower()
        if ":" not in normalized:
            return False
        intent_code = normalized.split(":", 1)[0]
        return intent_code in BALANCE_INTENT_MEMORY_PREFIXES

    def _extract_balance_card_number(self, text: str | None) -> str | None:
        if not text:
            return None
        explicit_match = EXPLICIT_BALANCE_CARD_RE.search(text)
        if explicit_match:
            return explicit_match.group(1)
        stripped = text.strip()
        if stripped.isdigit():
            return stripped
        generic_match = CARD_NUMBER_RE.search(text)
        if generic_match and not any(marker in text for marker in ("收款", "对方", "目标")):
            return generic_match.group(1)
        return None

    def _extract_balance_phone_last4(self, text: str | None) -> str | None:
        if not text:
            return None
        explicit_match = EXPLICIT_BALANCE_PHONE_RE.search(text)
        if explicit_match:
            return explicit_match.group(1)
        mobile_match = PHONE_NUMBER_RE.search(text)
        if mobile_match and not any(marker in text for marker in ("收款", "对方", "目标")):
            return mobile_match.group(0)[-4:]
        exact_match = FOUR_DIGITS_ONLY_RE.match(text.strip())
        if exact_match:
            return exact_match.group(1)
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
            return "请提供卡号和手机号后4位"
        if missing == ["card_number"]:
            return "请提供卡号"
        if missing == ["phone_last_four"]:
            return "请提供手机号后4位"
        return "请提供卡号和手机号后4位"
