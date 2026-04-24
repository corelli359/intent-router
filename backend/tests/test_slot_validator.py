from __future__ import annotations

from router_service.core.shared.domain import IntentDefinition
from router_service.core.slots.validator import SlotValidator
from router_service.core.shared.graph_domain import SlotBindingSource, SlotBindingState


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
                "label": "收款人姓名",
                "description": "当前转账的收款人姓名",
                "aliases": ["收款人", "对方姓名"],
                "value_type": "string",
                "required": True,
            },
            {
                "slot_key": "amount",
                "label": "转账金额",
                "description": "当前转账金额",
                "value_type": "currency",
                "required": True,
            },
        ],
    )


def _binding(slot_key: str, value: str, source_text: str) -> SlotBindingState:
    return SlotBindingState(
        slot_key=slot_key,
        value=value,
        source=SlotBindingSource.USER_MESSAGE,
        source_text=source_text,
        confidence=0.9,
    )


def test_slot_validator_reports_missing_required_slot() -> None:
    validator = SlotValidator()
    result = validator.validate(
        intent=_gas_intent(),
        slot_memory={"gas_account_number": "88001234"},
        slot_bindings=[_binding("gas_account_number", "88001234", "给燃气户号88001234")],
        history_slot_keys=[],
        ambiguous_slot_keys=[],
        graph_source_message="给燃气户号88001234",
        node_source_fragment="给燃气户号88001234",
        current_message="给燃气户号88001234",
        long_term_memory=[],
    )

    assert result.missing_required_slots == ["amount"]
    assert result.can_dispatch is False
    assert "amount" not in result.slot_memory


def test_slot_validator_accepts_grounded_slots() -> None:
    validator = SlotValidator()
    result = validator.validate(
        intent=_gas_intent(),
        slot_memory={"gas_account_number": "88001234", "amount": "88"},
        slot_bindings=[
            _binding("gas_account_number", "88001234", "燃气户号88001234"),
            _binding("amount", "88", "交88元"),
        ],
        history_slot_keys=[],
        ambiguous_slot_keys=[],
        graph_source_message="给燃气户号88001234缴费88元",
        node_source_fragment="给燃气户号88001234缴费88元",
        current_message="给燃气户号88001234缴费88元",
        long_term_memory=[],
    )

    assert result.can_dispatch is True
    assert result.missing_required_slots == []
    assert result.slot_memory == {"gas_account_number": "88001234", "amount": "88"}


def test_slot_validator_keeps_user_message_binding_without_source_text_grounding() -> None:
    validator = SlotValidator()
    result = validator.validate(
        intent=_transfer_intent(),
        slot_memory={"payee_name": "小明"},
        slot_bindings=[_binding("payee_name", "小明", "给小明转账")],
        history_slot_keys=[],
        ambiguous_slot_keys=[],
        graph_source_message="我要转账",
        node_source_fragment="我要转账",
        current_message="我要转账",
        long_term_memory=[],
    )

    assert result.can_dispatch is False
    assert result.invalid_slot_keys == []
    assert result.missing_required_slots == ["amount"]
    assert result.slot_memory == {"payee_name": "小明"}


def test_slot_validator_preserves_previously_grounded_user_message_binding_across_turns() -> None:
    validator = SlotValidator()
    result = validator.validate(
        intent=_transfer_intent(),
        slot_memory={"payee_name": "小红", "amount": "200"},
        slot_bindings=[
            _binding("payee_name", "小红", "小红吧"),
            _binding("amount", "200", "金额200"),
        ],
        history_slot_keys=[],
        ambiguous_slot_keys=[],
        graph_source_message="我要转账",
        node_source_fragment="我要转账",
        current_message="金额200",
        recent_messages=["我要转账", "小红吧", "金额200"],
        long_term_memory=[],
    )

    assert result.can_dispatch is True
    assert result.invalid_slot_keys == []
    assert result.slot_memory == {"payee_name": "小红", "amount": "200"}


def test_slot_validator_accepts_source_text_backed_normalized_numeric_slot() -> None:
    validator = SlotValidator()
    result = validator.validate(
        intent=_gas_intent(),
        slot_memory={"gas_account_number": "88001234", "amount": "4321"},
        slot_bindings=[
            _binding("gas_account_number", "88001234", "燃气户号88001234"),
            _binding("amount", "4321", "肆叁贰壹元"),
        ],
        history_slot_keys=[],
        ambiguous_slot_keys=[],
        graph_source_message="帮我交燃气费",
        node_source_fragment="帮我交燃气费",
        current_message="肆叁贰壹元",
        recent_messages=["帮我交燃气费", "燃气户号88001234", "肆叁贰壹元"],
        long_term_memory=[],
    )

    assert result.can_dispatch is True
    assert result.invalid_slot_keys == []
    assert result.slot_memory == {"gas_account_number": "88001234", "amount": "4321"}
