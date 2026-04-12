from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from router_service.core.shared.domain import IntentDefinition
from router_service.core.slots.grounding import slot_value_grounded
from router_service.core.shared.graph_domain import SlotBindingSource, SlotBindingState
from router_service.models.intent import SlotValueType


_CURRENCY_TOKENS = {
    "CNY": ("人民币", "CNY"),
    "USD": ("美元", "USD"),
    "HKD": ("港币", "港元", "HKD"),
    "EUR": ("欧元", "EUR"),
    "JPY": ("日元", "JPY"),
}


@dataclass(slots=True)
class SlotValidationResult:
    slot_memory: dict[str, Any]
    slot_bindings: list[SlotBindingState]
    history_slot_keys: list[str]
    missing_required_slots: list[str]
    ambiguous_slot_keys: list[str]
    invalid_slot_keys: list[str]
    needs_confirmation: bool
    can_dispatch: bool
    prompt_message: str | None


class SlotValidator:
    def validate(
        self,
        *,
        intent: IntentDefinition,
        slot_memory: dict[str, Any],
        slot_bindings: list[SlotBindingState],
        history_slot_keys: list[str],
        ambiguous_slot_keys: list[str],
        graph_source_message: str,
        node_source_fragment: str | None,
        current_message: str,
        long_term_memory: list[str] | None = None,
    ) -> SlotValidationResult:
        slot_defs_by_key = {slot.slot_key: slot for slot in intent.slot_schema}
        binding_by_key = {binding.slot_key: binding for binding in slot_bindings}
        grounding_text = self._combined_text(graph_source_message, node_source_fragment, current_message)
        history_text = "\n".join(entry for entry in (long_term_memory or []) if entry)

        validated_memory: dict[str, Any] = {}
        validated_bindings: list[SlotBindingState] = []
        invalid_slot_keys: list[str] = []

        for slot_key, value in slot_memory.items():
            slot_def = slot_defs_by_key.get(slot_key)
            if slot_def is None:
                continue
            binding = binding_by_key.get(slot_key)
            source = (
                binding.source
                if binding is not None
                else SlotBindingSource.HISTORY
                if slot_key in history_slot_keys
                else SlotBindingSource.USER_MESSAGE
            )
            if not self._binding_is_valid(
                slot_def=slot_def,
                value=value,
                source=source,
                source_text=binding.source_text if binding is not None else None,
                grounding_text=grounding_text,
                history_text=history_text,
            ):
                invalid_slot_keys.append(slot_key)
                continue
            validated_memory[slot_key] = value
            validated_bindings.append(
                binding
                or SlotBindingState(
                    slot_key=slot_key,
                    value=value,
                    source=source,
                    source_text=node_source_fragment or current_message or graph_source_message,
                    confidence=None,
                )
            )

        missing_required_slots = [
            slot.slot_key
            for slot in intent.slot_schema
            if slot.required and slot.slot_key not in validated_memory
        ]
        unresolved_ambiguous = [
            slot_key
            for slot_key in ambiguous_slot_keys
            if slot_key in slot_defs_by_key and slot_key not in validated_memory
        ]
        prompt_message = self._build_prompt_message(
            intent=intent,
            missing_required_slots=missing_required_slots,
            ambiguous_slot_keys=unresolved_ambiguous,
            invalid_slot_keys=invalid_slot_keys,
        )
        can_dispatch = not missing_required_slots and not unresolved_ambiguous and not invalid_slot_keys
        return SlotValidationResult(
            slot_memory=validated_memory,
            slot_bindings=validated_bindings,
            history_slot_keys=[slot_key for slot_key in history_slot_keys if slot_key in validated_memory],
            missing_required_slots=missing_required_slots,
            ambiguous_slot_keys=unresolved_ambiguous,
            invalid_slot_keys=invalid_slot_keys,
            needs_confirmation=False,
            can_dispatch=can_dispatch,
            prompt_message=prompt_message,
        )

    def _binding_is_valid(
        self,
        *,
        slot_def,
        value: Any,
        source: SlotBindingSource,
        source_text: str | None,
        grounding_text: str,
        history_text: str,
    ) -> bool:
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if source == SlotBindingSource.RECOMMENDATION:
            return slot_def.allow_from_recommendation
        if source == SlotBindingSource.HISTORY:
            if not slot_def.allow_from_history:
                return False
            evidence_text = self._combined_text(source_text, history_text)
            return bool(evidence_text) and self._value_is_grounded(
                slot_def=slot_def,
                value=value,
                grounding_text=evidence_text,
            )
        evidence_text = source_text or grounding_text
        return bool(evidence_text) and self._value_is_grounded(
            slot_def=slot_def,
            value=value,
            grounding_text=evidence_text,
        )

    def _build_prompt_message(
        self,
        *,
        intent: IntentDefinition,
        missing_required_slots: list[str],
        ambiguous_slot_keys: list[str],
        invalid_slot_keys: list[str],
    ) -> str | None:
        labels_by_key = {
            slot.slot_key: slot.label or slot.description or slot.slot_key
            for slot in intent.slot_schema
        }
        parts: list[str] = []
        if missing_required_slots:
            labels = "、".join(labels_by_key.get(slot_key, slot_key) for slot_key in missing_required_slots)
            parts.append(f"请提供{labels}")
        if ambiguous_slot_keys:
            labels = "、".join(labels_by_key.get(slot_key, slot_key) for slot_key in ambiguous_slot_keys)
            parts.append(f"请明确{labels}")
        if invalid_slot_keys:
            labels = "、".join(labels_by_key.get(slot_key, slot_key) for slot_key in invalid_slot_keys)
            parts.append(f"请重新提供{labels}")
        if not parts:
            return None
        return "；".join(parts)

    def _combined_text(self, *parts: str | None) -> str:
        ordered_parts: list[str] = []
        for part in parts:
            cleaned = (part or "").strip()
            if not cleaned or cleaned in ordered_parts:
                continue
            ordered_parts.append(cleaned)
        return "\n".join(ordered_parts)

    def _value_is_grounded(self, *, slot_def, value: Any, grounding_text: str) -> bool:
        if slot_value_grounded(slot_def=slot_def, value=value, grounding_text=grounding_text):
            return True
        if slot_def.value_type != SlotValueType.STRING:
            return False
        slot_signature = " ".join(
            part.lower()
            for part in (
                slot_def.slot_key,
                slot_def.label,
                slot_def.description,
                slot_def.semantic_definition,
                " ".join(slot_def.aliases),
            )
            if part
        )
        if "币种" not in slot_signature and "currency" not in slot_signature:
            return False
        currency_code = str(value).upper()
        aliases = _CURRENCY_TOKENS.get(currency_code)
        if not aliases:
            return False
        upper_text = grounding_text.upper()
        return any(alias.upper() in upper_text or alias in grounding_text for alias in aliases)
