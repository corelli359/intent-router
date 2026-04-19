from __future__ import annotations

from decimal import Decimal, InvalidOperation


_CURRENCY_ALIASES = {
    "人民币": "CNY",
    "rmb": "CNY",
    "cny": "CNY",
    "美元": "USD",
    "美金": "USD",
    "usd": "USD",
    "欧元": "EUR",
    "eur": "EUR",
    "日元": "JPY",
    "jpy": "JPY",
    "港币": "HKD",
    "hkd": "HKD",
    "英镑": "GBP",
    "gbp": "GBP",
}


def normalize_digits(value: str | int | float | None) -> str | None:
    if value is None:
        return None
    digits = "".join(character for character in str(value) if character.isdigit())
    return digits or None


def normalize_account_number(value: str | int | None) -> str | None:
    return normalize_digits(value)


def normalize_phone_last4(value: str | int | None) -> str | None:
    digits = normalize_digits(value)
    if digits is None or len(digits) < 4:
        return None
    return digits[-4:]


def digit_runs(text: str) -> list[str]:
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


def normalize_amount(value: str | int | float | Decimal | None) -> str | None:
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


def amount_value(value: str | int | float | Decimal | None) -> Decimal | None:
    normalized = normalize_amount(value)
    if normalized is None:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def format_decimal(value: Decimal) -> str:
    if value == value.to_integral():
        return format(value.quantize(Decimal("1")), "f")
    return format(value.normalize(), "f")


def find_currency_mentions(text: str) -> list[tuple[int, str]]:
    lowered = text.lower()
    mentions: list[tuple[int, str]] = []
    for alias, code in _CURRENCY_ALIASES.items():
        start = 0
        alias_lower = alias.lower()
        while True:
            index = lowered.find(alias_lower, start)
            if index < 0:
                break
            mentions.append((index, code))
            start = index + len(alias_lower)
    mentions.sort(key=lambda item: item[0])
    return mentions
