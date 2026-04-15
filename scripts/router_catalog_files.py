"""Helpers for exporting split router file-catalog artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


INTENTS_FILE_NAME = "intents.json"
FIELD_CATALOGS_FILE_NAME = "field-catalogs.json"
SLOT_SCHEMAS_FILE_NAME = "slot-schemas.json"
GRAPH_BUILD_HINTS_FILE_NAME = "graph-build-hints.json"


def split_catalog_payloads(
    intents: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[Any]], dict[str, list[Any]], dict[str, dict[str, Any]]]:
    """Split full intent payloads into base intent records plus optional overlay maps."""
    base_intents: list[dict[str, Any]] = []
    field_catalogs: dict[str, list[Any]] = {}
    slot_schemas: dict[str, list[Any]] = {}
    graph_build_hints: dict[str, dict[str, Any]] = {}

    for raw_intent in intents:
        intent = dict(raw_intent)
        intent_code = str(intent.get("intent_code", "")).strip()
        if not intent_code:
            raise RuntimeError("intent payload is missing intent_code")
        field_catalogs[intent_code] = list(intent.pop("field_catalog", []) or [])
        slot_schemas[intent_code] = list(intent.pop("slot_schema", []) or [])
        graph_build_hints[intent_code] = dict(intent.pop("graph_build_hints", {}) or {})
        base_intents.append(intent)

    return base_intents, field_catalogs, slot_schemas, graph_build_hints


def write_split_catalog(
    output_dir: Path,
    *,
    intents: Iterable[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write the split file catalog layout used by router file mode."""
    output_dir.mkdir(parents=True, exist_ok=True)
    base_intents, field_catalogs, slot_schemas, graph_build_hints = split_catalog_payloads(intents)

    intents_payload = {"intents": base_intents}
    if metadata:
        intents_payload.update(metadata)

    _write_json(output_dir / INTENTS_FILE_NAME, intents_payload)
    _write_json(output_dir / FIELD_CATALOGS_FILE_NAME, {"field_catalogs": field_catalogs})
    _write_json(output_dir / SLOT_SCHEMAS_FILE_NAME, {"slot_schemas": slot_schemas})
    _write_json(output_dir / GRAPH_BUILD_HINTS_FILE_NAME, {"graph_build_hints": graph_build_hints})


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON payload using repository-standard formatting."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
