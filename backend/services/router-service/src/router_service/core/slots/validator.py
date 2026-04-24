from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.diagnostics import (
    RouterDiagnostic,
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.slots.grounding import (
    combine_distinct_text,
    grounded_source_text,
    slot_value_grounded_or_source_text_backed,
)
from router_service.core.shared.graph_domain import SlotBindingSource, SlotBindingState


@dataclass(slots=True)
class SlotValidationResult:
    """Validated slot state plus dispatch permission and user prompt information."""

    slot_memory: dict[str, Any]
    slot_bindings: list[SlotBindingState]
    history_slot_keys: list[str]
    missing_required_slots: list[str]
    ambiguous_slot_keys: list[str]
    invalid_slot_keys: list[str]
    needs_confirmation: bool
    can_dispatch: bool
    prompt_message: str | None
    diagnostics: list[RouterDiagnostic] | None = None


class SlotValidator:
    """Validate extracted slot candidates before the router dispatches a node."""

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
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> SlotValidationResult:
        """Validate extracted slots against grounding, history rules, and required fields."""
        slot_defs_by_key = {slot.slot_key: slot for slot in intent.slot_schema}
        binding_by_key = {binding.slot_key: binding for binding in slot_bindings}
        grounding_text = combine_distinct_text(graph_source_message, node_source_fragment, current_message)
        turn_history_text = combine_distinct_text(*(recent_messages or []), grounding_text)
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
                turn_history_text=turn_history_text,
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
        diagnostics = merge_diagnostics(
            [
                diagnostic(
                    RouterDiagnosticCode.SLOT_REQUIRED_MISSING,
                    source="slot_validator",
                    message="当前节点仍缺少必填槽位",
                    details={
                        "intent_code": intent.intent_code,
                        "missing_required_slots": missing_required_slots,
                    },
                )
            ]
            if missing_required_slots
            else [],
            [
                diagnostic(
                    RouterDiagnosticCode.SLOT_AMBIGUOUS,
                    source="slot_validator",
                    message="当前节点存在需要澄清的歧义槽位",
                    details={
                        "intent_code": intent.intent_code,
                        "ambiguous_slot_keys": unresolved_ambiguous,
                    },
                )
            ]
            if unresolved_ambiguous
            else [],
            [
                diagnostic(
                    RouterDiagnosticCode.SLOT_INVALID,
                    source="slot_validator",
                    message="当前节点存在未通过校验的槽位",
                    details={
                        "intent_code": intent.intent_code,
                        "invalid_slot_keys": invalid_slot_keys,
                    },
                )
            ]
            if invalid_slot_keys
            else [],
        )
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
            diagnostics=diagnostics,
        )

    def _binding_is_valid(
        self,
        *,
        slot_def,
        value: Any,
        source: SlotBindingSource,
        source_text: str | None,
        grounding_text: str,
        turn_history_text: str,
        history_text: str,
    ) -> bool:
        """Check whether one binding source/value pair may be trusted for dispatch."""
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if source == SlotBindingSource.RECOMMENDATION:
            return slot_def.allow_from_recommendation
        if source == SlotBindingSource.HISTORY:
            if not slot_def.allow_from_history:
                return False
            trusted_source_text = grounded_source_text(source_text, history_text)
            evidence_text = combine_distinct_text(
                trusted_source_text,
                history_text,
            )
            return bool(evidence_text) and self._value_is_grounded(
                slot_def=slot_def,
                value=value,
                grounding_text=evidence_text,
                source_text=trusted_source_text,
            )
        trusted_source_text = grounded_source_text(source_text, turn_history_text)
        evidence_text = trusted_source_text or grounding_text
        return bool(evidence_text) and self._value_is_grounded(
            slot_def=slot_def,
            value=value,
            grounding_text=evidence_text,
            source_text=trusted_source_text,
        )

    def _build_prompt_message(
        self,
        *,
        intent: IntentDefinition,
        missing_required_slots: list[str],
        ambiguous_slot_keys: list[str],
        invalid_slot_keys: list[str],
    ) -> str | None:
        """Build the user-facing follow-up prompt for missing or invalid slots."""
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

    def _value_is_grounded(
        self,
        *,
        slot_def,
        value: Any,
        grounding_text: str,
        source_text: str | None,
    ) -> bool:
        """Apply grounding logic, including currency-specific fallback matching."""
        return slot_value_grounded_or_source_text_backed(
            slot_def=slot_def,
            value=value,
            grounding_text=grounding_text,
            source_text=source_text,
        )
