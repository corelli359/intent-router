from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path
from typing import Any

from router_service.catalog.file_intent_repository import FileIntentRepository


DEFAULT_FALLBACK_AGENT_URL = "http://intent-fallback-agent.intent.svc.cluster.local:8000/api/agent/run"

COMMON_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["sessionId", "taskId", "input"],
}

COMMON_CONTEXT_MAPPING = {
    "sessionId": "$session.id",
    "taskId": "$task.id",
    "input": "$message.current",
    "conversation.recentMessages": "$context.recent_messages",
    "conversation.longTermMemory": "$context.long_term_memory",
}

_EXAMPLE_SPLIT_PATTERN = re.compile(r"[：:；;\n]+")


def build_csv_catalog_payloads(
    *,
    csv_path: str | Path,
    existing_catalog_dir: str | Path,
    fallback_agent_url: str = DEFAULT_FALLBACK_AGENT_URL,
) -> list[dict[str, Any]]:
    """Build router intent payloads from the business CSV plus the current transfer config."""
    rows = _load_csv_rows(Path(csv_path).expanduser().resolve())
    existing_payloads = load_existing_catalog_payloads(existing_catalog_dir)
    transfer_payload = existing_payloads.get("AG_TRANS") or existing_payloads.get("transfer_money")
    if transfer_payload is None:
        raise RuntimeError("Existing catalog does not contain AG_TRANS or transfer_money")

    counts = Counter(row["intent_code"] for row in rows)
    payloads: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        unique_code = unique_intent_code(
            raw_code=row["intent_code"],
            row_num=row["row_num"],
            duplicate_count=counts[row["intent_code"]],
        )
        dispatch_priority = max(1, 1000 - index)
        if row["intent_code"] == "AG_TRANS":
            payloads.append(
                _build_transfer_payload(
                    row=row,
                    unique_code=unique_code,
                    dispatch_priority=dispatch_priority,
                    transfer_payload=transfer_payload,
                )
            )
            continue
        payloads.append(
            _build_generic_payload(
                row=row,
                unique_code=unique_code,
                dispatch_priority=dispatch_priority,
                fallback_agent_url=fallback_agent_url,
            )
        )
    return payloads


def load_existing_catalog_payloads(catalog_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Load the merged file-mode catalog payloads keyed by intent code."""
    catalog_dir = Path(catalog_dir).expanduser().resolve()
    repository = FileIntentRepository(
        catalog_dir / "intents.json",
        field_catalog_path=catalog_dir / "field-catalogs.json",
        slot_schema_path=catalog_dir / "slot-schemas.json",
        graph_build_hints_path=catalog_dir / "graph-build-hints.json",
    )
    return {
        record.intent_code: record.model_dump(mode="json")
        for record in repository.list_intents()
    }


def unique_intent_code(*, raw_code: str, row_num: str, duplicate_count: int) -> str:
    """Return a stable unique intent code for one CSV row."""
    normalized_code = raw_code.strip()
    normalized_row_num = row_num.strip()
    if duplicate_count <= 1:
        return normalized_code
    return f"{normalized_code}_{normalized_row_num}"


def parse_examples(raw_examples: str) -> list[str]:
    """Split the screenshot-derived example column into distinct example utterances."""
    normalized = raw_examples.strip()
    if not normalized:
        return []
    parts = [item.strip() for item in _EXAMPLE_SPLIT_PATTERN.split(normalized)]
    return [item for item in parts if item]


def _load_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    """Load the business CSV into normalized row dictionaries."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        normalized_rows.append(
            {
                "row_num": str(row.get("row_num", "")).strip(),
                "intent_code": str(row.get("intent_code", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "category": str(row.get("category", "")).strip(),
                "description": str(row.get("description", "")).strip(),
                "examples": str(row.get("examples", "")).strip(),
            }
        )
    return normalized_rows


def _build_transfer_payload(
    *,
    row: dict[str, str],
    unique_code: str,
    dispatch_priority: int,
    transfer_payload: dict[str, Any],
) -> dict[str, Any]:
    """Build the AG_TRANS payload while preserving the existing transfer slot contract."""
    examples = parse_examples(row["examples"]) or transfer_payload.get("examples", [])
    graph_build_hints = dict(transfer_payload.get("graph_build_hints", {}) or {})
    provides_context_keys = list(graph_build_hints.get("provides_context_keys") or [])
    for key in ("amount", "business_status"):
        if key not in provides_context_keys:
            provides_context_keys.append(key)
    graph_build_hints["provides_context_keys"] = provides_context_keys

    return {
        "intent_code": unique_code,
        "name": row["name"] or transfer_payload.get("name", "转账"),
        "description": row["description"] or transfer_payload.get("description", ""),
        "domain_code": unique_code.lower(),
        "domain_name": row["category"] or row["name"] or transfer_payload.get("domain_name", ""),
        "domain_description": row["description"] or transfer_payload.get("domain_description", ""),
        "examples": examples,
        "routing_examples": examples,
        "agent_url": transfer_payload["agent_url"],
        "is_leaf_intent": True,
        "parent_intent_code": "",
        "status": "active",
        "is_fallback": False,
        "dispatch_priority": dispatch_priority,
        "request_schema": dict(transfer_payload.get("request_schema", COMMON_REQUEST_SCHEMA)),
        "field_mapping": dict(transfer_payload.get("field_mapping", COMMON_CONTEXT_MAPPING)),
        "field_catalog": list(transfer_payload.get("field_catalog", [])),
        "slot_schema": list(transfer_payload.get("slot_schema", [])),
        "graph_build_hints": graph_build_hints,
        "resume_policy": str(transfer_payload.get("resume_policy", "resume_same_task")),
    }


def _build_generic_payload(
    *,
    row: dict[str, str],
    unique_code: str,
    dispatch_priority: int,
    fallback_agent_url: str,
) -> dict[str, Any]:
    """Build a recognition-first payload for non-transfer intents."""
    examples = parse_examples(row["examples"])
    graph_build_hints = {
        "planner_notes": "当前仅启用意图识别，尚未配置专属提槽与执行逻辑。",
        "confirm_policy": "always",
        "max_nodes_per_message": 1,
    }
    return {
        "intent_code": unique_code,
        "name": row["name"] or unique_code,
        "description": row["description"] or row["name"] or unique_code,
        "domain_code": unique_code.lower(),
        "domain_name": row["category"] or row["name"] or unique_code,
        "domain_description": row["description"] or row["category"] or row["name"] or unique_code,
        "examples": examples,
        "routing_examples": examples,
        "agent_url": fallback_agent_url,
        "is_leaf_intent": True,
        "parent_intent_code": "",
        "status": "active",
        "is_fallback": False,
        "dispatch_priority": dispatch_priority,
        "request_schema": dict(COMMON_REQUEST_SCHEMA),
        "field_mapping": dict(COMMON_CONTEXT_MAPPING),
        "field_catalog": [],
        "slot_schema": [],
        "graph_build_hints": graph_build_hints,
        "resume_policy": "resume_same_task",
    }
