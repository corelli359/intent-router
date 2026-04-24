from __future__ import annotations

import asyncio
import json

from router_service.core.shared.domain import IntentDefinition
from router_service.core.slots.extractor import SlotExtractor
from router_service.core.shared.graph_domain import GraphNodeState, SlotBindingSource, SlotBindingState


class _RetryableLLMError(Exception):
    status_code = 429


class _RetryableLLMClient:
    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - tiny stub
        raise _RetryableLLMError("rate limited")


class _SuccessfulLLMClient:
    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - tiny stub
        del prompt, variables, model, on_delta
        return {
            "slots": [
                {
                    "slot_key": "gas_account_number",
                    "value": "88001234",
                    "source": "user_message",
                    "source_text": "燃气户号88001234",
                    "confidence": 0.96,
                },
                {
                    "slot_key": "amount",
                    "value": "88",
                    "source": "user_message",
                    "source_text": "交88元",
                    "confidence": 0.94,
                },
            ],
            "ambiguousSlotKeys": [],
        }


class _CapturingLLMClient:
    def __init__(self) -> None:
        self.variables: dict[str, object] | None = None

    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - tiny stub
        del prompt, model, on_delta
        self.variables = dict(variables)
        return {
            "slots": [],
            "ambiguousSlotKeys": [],
        }


class _HallucinatingTransferLLMClient:
    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - tiny stub
        del prompt, variables, model, on_delta
        return {
            "slots": [
                {
                    "slot_key": "payee_name",
                    "value": "小明",
                    "source": "user_message",
                    "source_text": "给小明转账",
                    "confidence": 0.99,
                },
                {
                    "slot_key": "amount",
                    "value": "200",
                    "source": "user_message",
                    "source_text": "转200元",
                    "confidence": 0.99,
                },
            ],
            "ambiguousSlotKeys": [],
        }


class _NormalizedNumericLLMClient:
    async def run_json(self, *, prompt, variables, model=None, on_delta=None):  # pragma: no cover - tiny stub
        del prompt, variables, model, on_delta
        return {
            "slots": [
                {
                    "slot_key": "amount",
                    "value": "4321",
                    "source": "user_message",
                    "source_text": "肆叁贰壹元",
                    "confidence": 0.98,
                }
            ],
            "ambiguousSlotKeys": [],
        }


def _gas_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="pay_gas_bill",
        name="缴纳燃气费",
        description="缴纳燃气费，需要燃气户号和缴费金额。",
        examples=["给燃气户号88001234交88元"],
        keywords=["燃气", "缴费"],
        agent_url="http://agent.example.com/gas",
        slot_schema=[
            {
                "slot_key": "gas_account_number",
                "label": "燃气户号",
                "description": "燃气缴费账户号",
                "value_type": "account_number",
                "required": True,
                "allow_from_history": True,
            },
            {
                "slot_key": "amount",
                "label": "缴费金额",
                "description": "本次缴费金额",
                "value_type": "currency",
                "required": True,
            },
        ],
    )


def _transfer_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="AG_TRANS",
        name="转账",
        description="执行转账，需要收款人姓名和金额。",
        examples=["给小明转500元"],
        keywords=["转账", "汇款"],
        agent_url="http://agent.example.com/transfer",
        slot_schema=[
            {
                "slot_key": "payee_name",
                "field_code": "payee_name",
                "label": "收款人姓名",
                "description": "当前转账的收款人姓名",
                "aliases": ["收款人", "对方姓名"],
                "value_type": "string",
                "required": True,
            },
            {
                "slot_key": "amount",
                "field_code": "amount",
                "label": "转账金额",
                "description": "当前转账金额",
                "value_type": "currency",
                "required": True,
            },
            {
                "slot_key": "payee_card_no",
                "label": "收款卡号",
                "aliases": ["收款卡号", "对方卡号"],
                "value_type": "string",
                "required": False,
            },
            {
                "slot_key": "payee_phone",
                "label": "收款手机号",
                "aliases": ["收款手机号", "对方手机号"],
                "value_type": "string",
                "required": False,
            },
        ],
    )


def test_slot_extractor_returns_structured_slots_from_llm() -> None:
    async def run() -> None:
        extractor = SlotExtractor(llm_client=_SuccessfulLLMClient())
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="给燃气户号88001234交88元",
            ),
            graph_source_message="给燃气户号88001234交88元",
            current_message="给燃气户号88001234交88元",
            long_term_memory=[],
        )

        assert result.slot_memory == {"gas_account_number": "88001234", "amount": "88"}
        assert result.ambiguous_slot_keys == []
        binding_by_key = {binding.slot_key: binding for binding in result.slot_bindings}
        assert binding_by_key["gas_account_number"].source == SlotBindingSource.USER_MESSAGE
        assert binding_by_key["gas_account_number"].source_text
        assert binding_by_key["amount"].value == "88"

    asyncio.run(run())


def test_slot_extractor_local_typed_parser_does_not_guess_semantic_string_slots() -> None:
    async def run() -> None:
        extractor = SlotExtractor()
        result = await extractor.extract(
            intent=_transfer_intent(),
            node=GraphNodeState(
                intent_code="AG_TRANS",
                title="转账",
                confidence=0.97,
                source_fragment="给小明转500元",
            ),
            graph_source_message="给小明转500元",
            current_message="给小明转500元",
            long_term_memory=[],
        )

        assert result.slot_memory == {"amount": "500"}
        assert "payee_name" not in result.slot_memory
        assert "payee_card_no" not in result.slot_memory
        assert "payee_phone" not in result.slot_memory

    asyncio.run(run())


def test_slot_extractor_preserves_history_bound_seed_slots() -> None:
    async def run() -> None:
        extractor = SlotExtractor()
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="帮我缴燃气费",
                slot_memory={"gas_account_number": "88001234"},
                history_slot_keys=["gas_account_number"],
            ),
            graph_source_message="帮我缴燃气费",
            current_message="帮我缴燃气费",
            long_term_memory=["gas_account_number=88001234"],
        )

        assert result.slot_memory == {"gas_account_number": "88001234"}
        assert result.history_slot_keys == ["gas_account_number"]

    asyncio.run(run())


def test_slot_extractor_keeps_empty_result_when_llm_is_rate_limited() -> None:
    async def run() -> None:
        extractor = SlotExtractor(llm_client=_RetryableLLMClient())
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="帮我交燃气费",
            ),
            graph_source_message="帮我交燃气费",
            current_message="燃气户号 88001234",
            long_term_memory=[],
        )

        assert result.slot_memory == {"gas_account_number": "88001234"}
        assert result.ambiguous_slot_keys == []
        assert result.diagnostics
        assert result.diagnostics[0].code == "SLOT_EXTRACTOR_LLM_RETRYABLE_UNAVAILABLE"

    asyncio.run(run())


def test_slot_extractor_passes_existing_slot_memory_into_llm_prompt() -> None:
    async def run() -> None:
        llm_client = _CapturingLLMClient()
        extractor = SlotExtractor(llm_client=llm_client)
        await extractor.extract(
            intent=_transfer_intent(),
            node=GraphNodeState(
                intent_code="AG_TRANS",
                title="转账",
                confidence=0.97,
                source_fragment="给小明转账",
                slot_memory={"payee_name": "小明"},
            ),
            graph_source_message="给小明转账",
            current_message="200",
            recent_messages=["user: 给小明转账", "assistant: 请提供金额"],
            long_term_memory=[],
        )

        assert llm_client.variables is not None
        assert json.loads(str(llm_client.variables["existing_slot_memory_json"])) == {"payee_name": "小明"}
        assert json.loads(str(llm_client.variables["recent_messages_json"])) == [
            "user: 给小明转账",
            "assistant: 请提供金额",
        ]

    asyncio.run(run())


def test_slot_extractor_keeps_llm_slots_without_source_text_grounding_validation() -> None:
    async def run() -> None:
        extractor = SlotExtractor(llm_client=_HallucinatingTransferLLMClient())
        result = await extractor.extract(
            intent=_transfer_intent(),
            node=GraphNodeState(
                intent_code="AG_TRANS",
                title="转账",
                confidence=0.97,
                source_fragment="我要转账",
            ),
            graph_source_message="我要转账",
            current_message="我要转账",
            long_term_memory=[],
        )

        assert result.slot_memory == {"payee_name": "小明", "amount": "200"}
        assert result.ambiguous_slot_keys == []

    asyncio.run(run())


def test_slot_extractor_preserves_previous_user_message_slot_across_turns() -> None:
    async def run() -> None:
        extractor = SlotExtractor()
        result = await extractor.extract(
            intent=_transfer_intent(),
            node=GraphNodeState(
                intent_code="AG_TRANS",
                title="转账",
                confidence=0.97,
                source_fragment="我要转账",
                slot_memory={"payee_name": "小红"},
                slot_bindings=[
                    SlotBindingState(
                        slot_key="payee_name",
                        value="小红",
                        source=SlotBindingSource.USER_MESSAGE,
                        source_text="小红吧",
                        confidence=0.95,
                    )
                ],
            ),
            graph_source_message="我要转账",
            current_message="金额200",
            recent_messages=["我要转账", "小红吧", "金额200"],
            long_term_memory=[],
        )

        assert result.slot_memory == {"payee_name": "小红", "amount": "200"}
        binding_by_key = {binding.slot_key: binding for binding in result.slot_bindings}
        assert binding_by_key["payee_name"].source_text == "小红吧"

    asyncio.run(run())


def test_slot_extractor_keeps_source_text_backed_normalized_numeric_slot() -> None:
    async def run() -> None:
        extractor = SlotExtractor(llm_client=_NormalizedNumericLLMClient())
        result = await extractor.extract(
            intent=_gas_intent(),
            node=GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳燃气费",
                confidence=0.95,
                source_fragment="帮我交燃气费",
                slot_memory={"gas_account_number": "88001234"},
                slot_bindings=[
                    SlotBindingState(
                        slot_key="gas_account_number",
                        value="88001234",
                        source=SlotBindingSource.USER_MESSAGE,
                        source_text="燃气户号88001234",
                        confidence=0.95,
                    )
                ],
            ),
            graph_source_message="帮我交燃气费",
            current_message="肆叁贰壹元",
            recent_messages=["帮我交燃气费", "燃气户号88001234", "肆叁贰壹元"],
            long_term_memory=[],
        )

        assert result.slot_memory == {"gas_account_number": "88001234", "amount": "4321"}
        binding_by_key = {binding.slot_key: binding for binding in result.slot_bindings}
        assert binding_by_key["amount"].source_text == "肆叁贰壹元"

    asyncio.run(run())
