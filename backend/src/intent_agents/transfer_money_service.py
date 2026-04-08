from __future__ import annotations

from decimal import Decimal, InvalidOperation
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


class TransferRecipient(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, alias="name")
    card_number: str | int | None = Field(default=None, alias="cardNumber")
    phone_last4: str | int | None = Field(default=None, alias="phoneLast4")


class TransferDetails(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    amount: str | int | float | None = Field(default=None, alias="amount")


class TransferMoneyAgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")
    input: str
    customer: AgentCustomer = Field(default_factory=AgentCustomer)
    conversation: AgentConversationContext = Field(default_factory=AgentConversationContext)
    intent: AgentIntentContext = Field(default_factory=AgentIntentContext)
    recipient: TransferRecipient = Field(default_factory=TransferRecipient)
    transfer: TransferDetails = Field(default_factory=TransferDetails)


class TransferMoneyResolution(BaseModel):
    recipient_name: str | None = None
    recipient_card_number: str | None = None
    recipient_phone_last4: str | None = None
    amount: str | None = None
    has_enough_information: bool = False
    ask_message: str = "请提供收款人姓名、收款卡号、收款人手机号后4位、转账金额"


_RECIPIENT_NAME_SLOT = IntentSlotDefinition(slot_key="recipient_name", value_type=SlotValueType.PERSON_NAME)
_RECIPIENT_CARD_SLOT = IntentSlotDefinition(slot_key="recipient_card_number", value_type=SlotValueType.ACCOUNT_NUMBER)
_RECIPIENT_PHONE_SLOT = IntentSlotDefinition(
    slot_key="recipient_phone_last4",
    value_type=SlotValueType.PHONE_LAST4,
)
_AMOUNT_SLOT = IntentSlotDefinition(slot_key="amount", value_type=SlotValueType.CURRENCY)


TRANSFER_MONEY_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "你是银行转账场景里的要素填充器。"
                "上游已经把请求路由到了 transfer_money。"
                "你不做意图识别，不执行转账，只做槽位提取和下一步追问。"
                "你必须只输出 JSON，不能输出解释。"
                "当前任务只关心四个槽位：recipient_name、recipient_card_number、recipient_phone_last4、amount。"
                "规则："
                "1. 优先保留 current_slots 里已经确认的值；当前输入只补充缺失槽位时，不能重置已有槽位。"
                "2. 只有用户明确修正某个槽位时，才覆盖那个槽位。"
                "3. 亲属关系或常用称呼也可以直接作为 recipient_name，例如“弟弟”“我弟弟”“媳妇儿”“我妈”“爸爸”。"
                "4. 像“给我弟弟转500”“给弟弟转账500”“转500给我媳妇儿”这种表达，"
                "如果当前消息里已经明确包含收款人和金额，必须提取 recipient_name 与 amount。"
                "5. 金额不一定带“元”字；“转500”“转账500”“500块”都可以识别为 amount='500'。"
                "6. 不要从余额查询或其他无关意图历史中推断收款卡号、收款人手机号后4位或金额。"
                "7. recent_messages 和 long_term_memory 只能帮助你理解用户是不是在补充当前转账任务，"
                "不能把历史里出现过但当前轮没有再次明确提供的新槽位直接当成当前确认输入。"
                "8. 当前输入可能一次补充多个槽位；如果用户明确写了“卡号...，后四位....”，必须同时补齐 recipient_card_number 和 recipient_phone_last4。"
                "9. 如果当前输入是独立的 4 位数字，且 recipient_phone_last4 和 amount 都缺失，不要擅自判定它属于哪一个槽位，而是保持这两个槽位为空并追问澄清。"
                "10. 如果当前只缺 amount，那么独立数字可以识别为 amount；如果当前只缺 recipient_phone_last4，那么独立 4 位数字可以识别为 recipient_phone_last4。"
                "11. 输出的 recipient_card_number 只保留卡号本身，recipient_phone_last4 只保留 4 位尾号，amount 只保留数值字符串。"
                "12. 如果信息不足，has_enough_information 必须为 false，并给出简洁明确的 ask_message。"
                "示例："
                "A. 首轮输入“我要给我弟弟转500”时，应返回 recipient_name='弟弟'、amount='500'。"
                "B. 首轮输入“帮我查一下余额，然后给我弟弟转账”，即使 current_input 同时包含别的诉求，"
                "也应从同一笔转账语义里识别 recipient_name='弟弟'。"
                "C. current_slots 已有 recipient_name='弟弟'、amount='500'，"
                "current_input='卡号61111111111111111，后四位9999' 时，应补 recipient_card_number='61111111111111111'、recipient_phone_last4='9999'。"
                "D. current_slots 已有 recipient_name='弟弟'，current_input='收款卡号 6222020100049999999' 时，"
                "应保留 recipient_name，并补 recipient_card_number。"
                "E. current_slots 已有 recipient_name='弟弟' 和 recipient_card_number='6222020100049999999'，"
                "current_input='手机号后四位 1234' 时，应保留前两项，并补 recipient_phone_last4='1234'。"
                "F. current_slots 已有 recipient_name、recipient_card_number、recipient_phone_last4，"
                "current_input='5000' 时，应只补 amount='5000'，不能清空已有槽位。"
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
                '  "recipient_name": "string | null",\n'
                '  "recipient_card_number": "string | null",\n'
                '  "recipient_phone_last4": "string | null",\n'
                '  "amount": "string | null",\n'
                '  "has_enough_information": true,\n'
                '  "ask_message": "string"\n'
                "}}"
            ),
        ),
    ]
)


class TransferMoneyAgentService:
    def __init__(self, *, resolver: JsonObjectRunner | None = None) -> None:
        self.resolver = resolver

    async def handle(self, request: TransferMoneyAgentRequest) -> AgentExecutionResponse:
        seeded = TransferMoneyResolution(
            recipient_name=self._normalize_name(request.recipient.name),
            recipient_card_number=self._normalize_card_number(request.recipient.card_number),
            recipient_phone_last4=self._normalize_phone_last4(request.recipient.phone_last4),
            amount=self._normalize_amount(request.transfer.amount),
        )
        direct_resolution = self._finalize_resolution(seeded, TransferMoneyResolution())
        if not request.input.strip() and direct_resolution.has_enough_information:
            resolution = direct_resolution
        else:
            resolution = await self._resolve(request, seeded)
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
                resolution.ask_message or self._ask_message(missing_fields),
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

    async def _resolve(
        self,
        request: TransferMoneyAgentRequest,
        seeded: TransferMoneyResolution,
    ) -> TransferMoneyResolution:
        if self.resolver is None:
            return self._finalize_resolution(seeded, TransferMoneyResolution())

        try:
            raw_payload = await self.resolver.run_json(
                prompt=TRANSFER_MONEY_PROMPT,
                variables={
                    "intent_json": dump_json(request.intent.model_dump()),
                    "input_text": request.input,
                    "current_slots_json": dump_json(
                        {
                            "recipient_name": request.recipient.name,
                            "recipient_card_number": request.recipient.card_number,
                            "recipient_phone_last4": request.recipient.phone_last4,
                            "amount": request.transfer.amount,
                        }
                    ),
                    "recent_messages_json": dump_json(request.conversation.recent_messages),
                    "long_term_memory_json": dump_json(request.conversation.long_term_memory),
                },
                schema=TransferMoneyResolution,
            )
            resolved = TransferMoneyResolution.model_validate(raw_payload)
        except Exception:
            resolved = TransferMoneyResolution()

        self._drop_unconfirmed_history_values(request, seeded, resolved)
        return self._finalize_resolution(seeded, resolved)

    def _drop_unconfirmed_history_values(
        self,
        request: TransferMoneyAgentRequest,
        seeded: TransferMoneyResolution,
        resolved: TransferMoneyResolution,
    ) -> None:
        if resolved.recipient_name and resolved.recipient_name != seeded.recipient_name:
            if not slot_value_grounded(
                slot_def=_RECIPIENT_NAME_SLOT,
                value=resolved.recipient_name,
                grounding_text=request.input,
            ):
                resolved.recipient_name = None
        if resolved.recipient_card_number and resolved.recipient_card_number != seeded.recipient_card_number:
            if not slot_value_grounded(
                slot_def=_RECIPIENT_CARD_SLOT,
                value=resolved.recipient_card_number,
                grounding_text=request.input,
            ):
                resolved.recipient_card_number = None
        if resolved.recipient_phone_last4 and resolved.recipient_phone_last4 != seeded.recipient_phone_last4:
            if not slot_value_grounded(
                slot_def=_RECIPIENT_PHONE_SLOT,
                value=resolved.recipient_phone_last4,
                grounding_text=request.input,
            ):
                resolved.recipient_phone_last4 = None
        if resolved.amount and resolved.amount != seeded.amount:
            if not slot_value_grounded(
                slot_def=_AMOUNT_SLOT,
                value=resolved.amount,
                grounding_text=request.input,
            ):
                resolved.amount = None

    def _finalize_resolution(
        self,
        seeded: TransferMoneyResolution,
        resolved: TransferMoneyResolution,
    ) -> TransferMoneyResolution:
        recipient_name = self._normalize_name(resolved.recipient_name) or seeded.recipient_name
        recipient_card_number = (
            self._normalize_card_number(resolved.recipient_card_number) or seeded.recipient_card_number
        )
        recipient_phone_last4 = (
            self._normalize_phone_last4(resolved.recipient_phone_last4) or seeded.recipient_phone_last4
        )
        amount = self._normalize_amount(resolved.amount) or seeded.amount
        has_enough_information = all(
            [
                recipient_name,
                recipient_card_number,
                recipient_phone_last4,
                amount,
            ]
        )
        ask_message = (resolved.ask_message or "").strip()
        missing_fields = self._missing_fields(
            TransferMoneyResolution(
                recipient_name=recipient_name,
                recipient_card_number=recipient_card_number,
                recipient_phone_last4=recipient_phone_last4,
                amount=amount,
            )
        )
        if not has_enough_information:
            ask_message = (
                ask_message
                if self._should_preserve_custom_ask_message(ask_message)
                else self._ask_message(missing_fields)
            )
        return TransferMoneyResolution(
            recipient_name=recipient_name,
            recipient_card_number=recipient_card_number,
            recipient_phone_last4=recipient_phone_last4,
            amount=amount,
            has_enough_information=bool(has_enough_information),
            ask_message=ask_message,
        )

    def _normalize_name(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

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

    def _normalize_amount(self, value: str | None) -> str | None:
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

    def _should_preserve_custom_ask_message(self, ask_message: str) -> bool:
        normalized = ask_message.strip()
        if not normalized:
            return False
        return "例如" in normalized or "请明确" in normalized or "检测到 4 位数字" in normalized

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
