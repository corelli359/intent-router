from __future__ import annotations

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


class BalanceAccount(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    card_number: str | int | None = Field(default=None, alias="cardNumber")
    phone_last4: str | int | None = Field(default=None, alias="phoneLast4")


class AccountBalanceAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    intent: AgentIntentContext = Field(default_factory=AgentIntentContext)
    account: BalanceAccount = Field(default_factory=BalanceAccount)


class AccountBalanceResolution(BaseModel):
    card_number: str | None = None
    phone_last4: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供卡号和手机号后4位"


ACCOUNT_BALANCE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是银行账户余额查询场景里的要素填充器。"
                "上游已经把请求路由到了 query_account_balance。"
                "你不做意图识别，不回答业务结果，不执行查询，只做槽位提取。"
                "你必须只输出 JSON，不能输出解释。"
                "当前任务只关心两个槽位：card_number 和 phone_last4。"
                "规则："
                "1. 优先保留 current_slots 里已经确认的值；当前输入只补充缺失槽位时，不能清空已有槽位。"
                "2. 用户可能一次性提供两个值，例如“6000000000,6666”，也可能分多轮补充。"
                "3. 不要猜测，不要从无关意图历史里补全敏感信息。"
                "4. 如果信息不足，has_enough_information 必须为 false，并给出简洁的 ask_message。"
                "5. 输出的 card_number 只保留卡号本身，输出的 phone_last4 只保留 4 位尾号。"
                "示例："
                "A. current_slots 为空，current_input='6000000000,6666' 时，"
                "应返回 card_number='6000000000'、phone_last4='6666'、has_enough_information=true。"
                "B. current_slots.card_number 已有值，current_input='1234' 时，"
                "应保留已有 card_number，并把 phone_last4 识别为 '1234'。"
            ),
        ),
        (
            "human",
            (
                "intent(JSON):\n{intent_json}\n\n"
                "current_input:\n{input_text}\n\n"
                "current_slots(JSON):\n{account_json}\n\n"
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


class AccountBalanceAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: AccountBalanceAgentRequest) -> AgentExecutionResponse:
        seeded = AccountBalanceResolution(
            card_number=self._normalize_card_number(request.account.card_number),
            phone_last4=self._normalize_phone_last4(request.account.phone_last4),
        )
        resolution = await self._resolve(request, seeded)
        slot_memory = self._slot_memory(resolution)

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
            resolution.ask_message,
            slot_memory=slot_memory,
            payload={
                "agent": "query_account_balance",
                "missing_fields": self._missing_fields(resolution.card_number, resolution.phone_last4),
            },
        )

    async def _resolve(
        self,
        request: AccountBalanceAgentRequest,
        seeded: AccountBalanceResolution,
    ) -> AccountBalanceResolution:
        heuristic = self._extract_from_input(request.input)
        if self.resolver is None:
            return self._finalize_resolution(seeded, heuristic)

        try:
            raw_payload = await self.resolver.run_json(
                prompt=ACCOUNT_BALANCE_PROMPT,
                variables={
                    "intent_json": dump_json(request.intent.model_dump()),
                    "input_text": request.input,
                    "account_json": dump_json(request.account.model_dump()),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                    "long_term_memory_json": dump_json(request.conversation.long_term_memory),
                },
                schema=AccountBalanceResolution,
            )
            resolved = AccountBalanceResolution.model_validate(raw_payload)
        except Exception:
            resolved = AccountBalanceResolution()

        if heuristic.card_number and not resolved.card_number:
            resolved.card_number = heuristic.card_number
        if heuristic.phone_last4 and not resolved.phone_last4:
            resolved.phone_last4 = heuristic.phone_last4

        return self._finalize_resolution(seeded, resolved)

    def _finalize_resolution(
        self,
        seeded: AccountBalanceResolution,
        resolved: AccountBalanceResolution,
    ) -> AccountBalanceResolution:
        card_number = self._normalize_card_number(resolved.card_number) or seeded.card_number
        phone_last4 = self._normalize_phone_last4(resolved.phone_last4) or seeded.phone_last4
        has_enough_information = bool(card_number and phone_last4)
        ask_message = "" if has_enough_information else self._ask_message(card_number, phone_last4)
        return AccountBalanceResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            has_enough_information=has_enough_information,
            ask_message=ask_message,
        )

    def _normalize_card_number(self, value: str | None) -> str | None:
        if value is None:
            return None
        digits = "".join(character for character in str(value) if character.isdigit())
        return digits or None

    def _normalize_phone_last4(self, value: str | None) -> str | None:
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
            return "请提供卡号和手机号后4位"
        if missing == ["card_number"]:
            return "请提供卡号"
        if missing == ["phone_last_four"]:
            return "请提供手机号后4位"
        return "请提供卡号和手机号后4位"

    def _slot_memory(self, resolution: AccountBalanceResolution) -> dict[str, Any]:
        slot_memory: dict[str, Any] = {}
        if resolution.card_number:
            slot_memory["card_number"] = resolution.card_number
        if resolution.phone_last4:
            slot_memory["phone_last_four"] = resolution.phone_last4
        return slot_memory

    def _extract_from_input(self, text: str) -> AccountBalanceResolution:
        digit_runs = self._digit_runs(text)
        card_number = next((run for run in digit_runs if 12 <= len(run) <= 19), None)
        phone_last4 = self._extract_phone_last4_from_runs(text, digit_runs, card_number)
        return AccountBalanceResolution(
            card_number=card_number,
            phone_last4=phone_last4,
            has_enough_information=bool(card_number and phone_last4),
            ask_message="",
        )

    def _extract_phone_last4_from_runs(
        self,
        text: str,
        digit_runs: list[str],
        card_number: str | None,
    ) -> str | None:
        explicit_phone_markers = ("尾号", "后4位", "后四位", "手机", "手机号")
        phone_candidates = [
            run[-4:]
            for run in digit_runs
            if run != card_number and 4 <= len(run) < 12
        ]
        if any(marker in text for marker in explicit_phone_markers):
            return phone_candidates[-1] if phone_candidates else None
        if phone_candidates:
            return phone_candidates[-1]
        if len(digit_runs) == 1 and len(digit_runs[0]) == 4:
            return digit_runs[0]
        return None

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
