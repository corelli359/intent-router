from __future__ import annotations

from typing import Any, Iterable

from router_service.models.intent import IntentSlotDefinition, SlotValueType


_DIGIT_VALUE_TYPES = {
    SlotValueType.NUMBER,
    SlotValueType.INTEGER,
    SlotValueType.CURRENCY,
    SlotValueType.ACCOUNT_NUMBER,
    SlotValueType.PHONE_LAST4,
    SlotValueType.IDENTIFIER,
}

CURRENCY_ALIASES_BY_CODE: dict[str, tuple[str, ...]] = {
    "CNY": ("人民币", "CNY"),
    "USD": ("美元", "USD"),
    "HKD": ("港币", "港元", "HKD"),
    "EUR": ("欧元", "EUR"),
    "JPY": ("日元", "JPY"),
}


def normalize_text(value: str) -> str:
    """Normalize text for loose containment checks during slot grounding."""
    return "".join(character for character in value.strip().lower() if not character.isspace())


def normalize_digits(value: str) -> str:
    """Extract only digits for number-like slot grounding checks."""
    return "".join(character for character in value if character.isdigit())


def combine_distinct_text(*parts: str | None) -> str:
    """Join distinct non-empty text fragments in stable order."""
    ordered_parts: list[str] = []
    for part in parts:
        cleaned = (part or "").strip()
        if not cleaned or cleaned in ordered_parts:
            continue
        ordered_parts.append(cleaned)
    return "\n".join(ordered_parts)


def slot_semantic_signature(slot_def: IntentSlotDefinition) -> str:
    """Build the semantic signature used by slot heuristics and grounding fallback."""
    return " ".join(
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


def slot_has_currency_semantics(slot_def: IntentSlotDefinition) -> bool:
    """Return whether one slot semantically represents a currency-like concept."""
    signature = slot_semantic_signature(slot_def)
    return "币种" in signature or "currency" in signature


def currency_aliases(currency_code: str) -> tuple[str, ...] | None:
    """Return known natural-language aliases for one currency code."""
    return CURRENCY_ALIASES_BY_CODE.get(currency_code.upper())


def slot_value_grounded(
    *,
    slot_def: IntentSlotDefinition | None,
    value: Any,
    grounding_text: str,
) -> bool:
    """Return whether one candidate slot value is grounded in the available text."""
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
    return any(candidate and candidate in normalized_grounding_text for candidate in candidates)


def grounded_source_text(source_text: str | None, grounding_text: str) -> str | None:
    """Return a trusted source-text snippet only when it is actually present in the evidence text."""
    cleaned_source_text = (source_text or "").strip()
    if not cleaned_source_text:
        return None

    normalized_source_text = normalize_text(cleaned_source_text)
    normalized_grounding_text = normalize_text(grounding_text)
    if normalized_source_text and normalized_source_text in normalized_grounding_text:
        return cleaned_source_text

    digits_source_text = normalize_digits(cleaned_source_text)
    digits_grounding_text = normalize_digits(grounding_text)
    if digits_source_text and digits_source_text in digits_grounding_text:
        return cleaned_source_text

    return None


def slot_value_grounded_with_currency_fallback(
    *,
    slot_def: IntentSlotDefinition,
    value: Any,
    grounding_text: str,
) -> bool:
    """Apply default grounding plus a currency-specific alias fallback."""
    if slot_value_grounded(slot_def=slot_def, value=value, grounding_text=grounding_text):
        return True
    if slot_def.value_type != SlotValueType.STRING or not slot_has_currency_semantics(slot_def):
        return False
    aliases = currency_aliases(str(value))
    if not aliases:
        return False
    upper_text = grounding_text.upper()
    return any(alias.upper() in upper_text or alias in grounding_text for alias in aliases)


def slot_value_grounded_or_source_text_backed(
    *,
    slot_def: IntentSlotDefinition,
    value: Any,
    grounding_text: str,
    source_text: str | None,
) -> bool:
    """Accept grounded values or trusted source-text-backed numeric normalizations."""
    if slot_value_grounded_with_currency_fallback(
        slot_def=slot_def,
        value=value,
        grounding_text=grounding_text,
    ):
        return True

    trusted_source_text = grounded_source_text(source_text, grounding_text)
    if not trusted_source_text:
        return False

    if slot_def.value_type in _DIGIT_VALUE_TYPES:
        normalized_value = normalize_digits(str(value).strip())
        return bool(normalized_value)

    return False


def normalize_slot_memory(
    *,
    slot_memory: dict[str, Any],
    slot_schema: Iterable[IntentSlotDefinition],
    grounding_text: str,
    history_texts: Iterable[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Filter slot memory down to grounded values and mark history-derived keys."""
    if not slot_memory:
        return {}, []

    normalized: dict[str, Any] = {}
    history_slot_keys: list[str] = []
    slot_schema_by_key = {slot.slot_key: slot for slot in slot_schema}
    combined_history_text = "\n".join(text for text in history_texts if text)

    for slot_key, raw_value in slot_memory.items():
        slot_def = slot_schema_by_key.get(slot_key)
        if slot_def is not None:
            grounded_in_turn = slot_value_grounded_with_currency_fallback(
                slot_def=slot_def,
                value=raw_value,
                grounding_text=grounding_text,
            )
        else:
            grounded_in_turn = slot_value_grounded(
                slot_def=slot_def,
                value=raw_value,
                grounding_text=grounding_text,
            )
        if grounded_in_turn:
            normalized[slot_key] = raw_value
            continue
        if (
            slot_def is not None
            and slot_def.allow_from_history
            and combined_history_text
            and slot_value_grounded_with_currency_fallback(
                slot_def=slot_def,
                value=raw_value,
                grounding_text=combined_history_text,
            )
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
    """Inject missing history-allowed slot values into the current slot memory."""
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
    """Clean structured slot payloads before they are stored on graph nodes."""
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
