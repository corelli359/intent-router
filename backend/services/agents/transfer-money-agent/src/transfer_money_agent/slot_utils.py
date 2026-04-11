from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SlotValueType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    CURRENCY = "currency"
    PERSON_NAME = "person_name"
    ACCOUNT_NUMBER = "account_number"
    PHONE_LAST4 = "phone_last4"
    IDENTIFIER = "identifier"


class SlotDefinition(BaseModel):
    slot_key: str
    value_type: SlotValueType = SlotValueType.STRING
    aliases: list[str] = Field(default_factory=list)


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
    slot_def: SlotDefinition | None,
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
