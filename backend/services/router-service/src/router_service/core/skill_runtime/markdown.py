from __future__ import annotations

import json
from typing import Any


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Parse a small frontmatter subset used by local markdown specs."""
    if not raw.startswith("---\n"):
        return {}, raw
    marker = "\n---\n"
    end = raw.find(marker, 4)
    if end < 0:
        return {}, raw
    frontmatter = raw[4:end]
    body = raw[end + len(marker) :]
    return parse_simple_yaml(frontmatter), body


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse single-line YAML scalars and arrays without adding a dependency."""
    payload: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        payload[key.strip()] = parse_scalar(raw_value.strip())
    return payload


def parse_scalar(raw: str) -> Any:
    if raw == "":
        return ""
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(item.strip()) for item in inner.split(",")]
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def extract_heading_section(markdown: str, heading: str, *, level: int = 2) -> str:
    """Return the text inside a markdown heading section."""
    marker = f"{'#' * level} {heading}"
    next_marker = f"{'#' * level} "
    lines = markdown.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == marker:
            start = index + 1
            break
    if start is None:
        return ""

    end = len(lines)
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith(next_marker):
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def extract_json_machine_spec(markdown: str) -> dict[str, Any]:
    """Load the first JSON code block in the `Machine Spec` section."""
    section = extract_heading_section(markdown, "Machine Spec")
    if not section:
        return {}
    fence = "```json"
    start = section.find(fence)
    if start < 0:
        return {}
    json_start = start + len(fence)
    end = section.find("```", json_start)
    if end < 0:
        return {}
    raw_json = section[json_start:end].strip()
    if not raw_json:
        return {}
    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise ValueError("Machine Spec JSON must be an object")
    return parsed
