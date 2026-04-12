from __future__ import annotations

from router_service.core.domain import IntentDefinition
from router_service.core.slot_validator import SlotValidator
from router_service.core.v2_domain import SlotBindingSource, SlotBindingState


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
