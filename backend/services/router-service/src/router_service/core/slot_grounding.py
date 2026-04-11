from __future__ import annotations

from typing import Any, Iterable

from intent_registry_contracts.models import IntentSlotDefinition, SlotValueType


_DIGIT_VALUE_TYPES = {
    SlotValueType.NUMBER,
    SlotValueType.INTEGER,
    SlotValueType.CURRENCY,
    SlotValueType.ACCOUNT_NUMBER,
    SlotValueType.PHONE_LAST4,
    SlotValueType.IDENTIFIER,
}


def normalize_text(value: str) -> str:
    return "".join(character for character in value.strip().lower() if not character.isspace())


def normalize_digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def slot_value_grounded(
    *,
    slot_def: IntentSlotDefinition | None,
    value: Any,
    grounding_text: str,
) -> bool:
    if value is None:
        return False
    string_value = str(value).strip()
    if not string_value:
        return False

    if slot_def is not None and slot_def.value_type in _DIGIT_VALUE_TYPES:
        normalized_value = normalize_digits(string_value)
        return bool(normalized_value and normalized_value in normalize_digits(grounding_text))

    normalized_grounding_text = normalize_text(grounding_text)
    candidates = {
        normalize_text(string_value),
        normalize_text(string_value.removeprefix("我")),
    }
    if slot_def is not None:
        candidates |= {normalize_text(alias) for alias in slot_def.aliases if alias}
    return any(candidate and candidate in normalized_grounding_text for candidate in candidates)


def normalize_slot_memory(
    *,
    slot_memory: dict[str, Any],
    slot_schema: Iterable[IntentSlotDefinition],
    grounding_text: str,
    history_texts: Iterable[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    if not slot_memory:
        return {}, []

    normalized: dict[str, Any] = {}
    history_slot_keys: list[str] = []
    slot_schema_by_key = {slot.slot_key: slot for slot in slot_schema}
    combined_history_text = "\n".join(text for text in history_texts if text)

    for slot_key, raw_value in slot_memory.items():
        slot_def = slot_schema_by_key.get(slot_key)
        if slot_value_grounded(slot_def=slot_def, value=raw_value, grounding_text=grounding_text):
            normalized[slot_key] = raw_value
            continue
        if (
            slot_def is not None
            and slot_def.allow_from_history
            and combined_history_text
            and slot_value_grounded(slot_def=slot_def, value=raw_value, grounding_text=combined_history_text)
        ):
            normalized[slot_key] = raw_value
            history_slot_keys.append(slot_key)
            continue
        if slot_def is None:
            normalized[slot_key] = raw_value

    return normalized, history_slot_keys


def apply_history_slot_values(
    *,
    slot_memory: dict[str, Any],
    slot_schema: Iterable[IntentSlotDefinition],
    history_slot_values: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    if not history_slot_values:
        return dict(slot_memory), []

    merged = dict(slot_memory)
    injected_slot_keys: list[str] = []
    for slot_def in slot_schema:
        if not slot_def.allow_from_history:
            continue
        if slot_def.slot_key in merged:
            continue
        value = history_slot_values.get(slot_def.slot_key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[slot_def.slot_key] = value
        injected_slot_keys.append(slot_def.slot_key)
    return merged, injected_slot_keys


def normalize_structured_slot_memory(
    *,
    slot_memory: dict[str, Any],
    slot_schema: Iterable[IntentSlotDefinition],
) -> dict[str, Any]:
    if not slot_memory:
        return {}

    normalized: dict[str, Any] = {}
    slot_schema_by_key = {slot.slot_key: slot for slot in slot_schema}
    for slot_key, raw_value in slot_memory.items():
        slot_def = slot_schema_by_key.get(slot_key)
        if isinstance(raw_value, str):
            cleaned = raw_value.strip()
            if not cleaned:
                continue
            normalized[slot_key] = cleaned
            continue
        if raw_value is None:
            continue
        if slot_def is None:
            normalized[slot_key] = raw_value
            continue
        normalized[slot_key] = raw_value
    return normalized
