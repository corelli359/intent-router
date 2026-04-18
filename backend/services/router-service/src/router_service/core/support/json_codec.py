from __future__ import annotations

from typing import Any

import orjson


JSONDecodeError = orjson.JSONDecodeError


def json_dumps(value: Any, *, sort_keys: bool = False) -> str:
    """Serialize a JSON-compatible value to UTF-8 text with orjson."""
    option = orjson.OPT_SORT_KEYS if sort_keys else 0
    return orjson.dumps(value, option=option).decode("utf-8")


def json_dumpb(value: Any, *, sort_keys: bool = False) -> bytes:
    """Serialize a JSON-compatible value to UTF-8 bytes with orjson."""
    option = orjson.OPT_SORT_KEYS if sort_keys else 0
    return orjson.dumps(value, option=option)


def json_loads(value: str | bytes | bytearray | memoryview) -> Any:
    """Deserialize a JSON document with orjson."""
    return orjson.loads(value)


def extract_first_json_value(raw_text: str) -> Any:
    """Extract the first valid JSON object or array from a text blob."""
    text = raw_text.strip()
    if not text:
        raise ValueError("LLM response is empty")

    try:
        return json_loads(text)
    except JSONDecodeError:
        pass

    for start, char in enumerate(text):
        if char not in "{[":
            continue
        end = _find_json_end(text, start)
        if end is None:
            continue
        try:
            return json_loads(text[start:end])
        except JSONDecodeError:
            continue
    raise ValueError(f"Could not find JSON payload in LLM response: {raw_text[:200]}")


def _find_json_end(text: str, start: int) -> int | None:
    """Return the end offset of one balanced top-level JSON object or array."""
    open_char = text[start]
    if open_char not in "{[":
        return None
    stack: list[str] = ["}" if open_char == "{" else "]"]
    in_string = False
    escaped = False

    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            stack.append("}")
            continue
        if char == "[":
            stack.append("]")
            continue
        if char not in "}]":
            continue
        if not stack or char != stack.pop():
            return None
        if not stack:
            return index + 1
    return None
