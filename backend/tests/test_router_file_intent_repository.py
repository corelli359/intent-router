from __future__ import annotations

import json
from pathlib import Path

import pytest

from router_service.catalog.file_intent_repository import FileIntentRepository
from router_service.catalog.intent_repository import (
    IntentRepositoryError,
    IntentRepositoryReadOnlyError,
)
from router_service.models.intent import IntentPayload, IntentStatus


def _catalog_payload() -> dict[str, object]:
    return {
        "intents": [
            {
                "intent_code": "transfer_money",
                "name": "转账",
                "description": "执行转账",
                "domain_code": "payment",
                "domain_name": "支付",
                "domain_description": "支付类意图",
                "examples": ["给小红转200"],
                "agent_url": "https://agent.example.com/transfer_money",
                "is_leaf_intent": True,
                "parent_intent_code": "",
                "status": "active",
                "routing_examples": ["帮我转账"],
                "is_fallback": False,
                "dispatch_priority": 200,
                "request_schema": {"type": "object"},
                "field_mapping": {"amount": "$slots.amount"},
                "field_catalog": [
                    {
                        "field_code": "amount",
                        "label": "金额",
                        "semantic_definition": "转账金额",
                        "value_type": "currency",
                        "examples": ["200元"],
                    }
                ],
                "slot_schema": [
                    {
                        "slot_key": "amount",
                        "field_code": "amount",
                        "role": "transfer_amount",
                        "label": "金额",
                        "description": "需要转出的金额",
                        "semantic_definition": "用户要转账的金额",
                        "value_type": "currency",
                        "required": True,
                        "aliases": ["金额"],
                        "examples": ["200元"],
                    }
                ],
                "graph_build_hints": {
                    "intent_scope_rule": "转账通常是一个独立动作",
                    "planner_notes": "如果同一轮出现多个对象或金额，需要拆成多节点",
                    "single_node_examples": ["给小红转200"],
                    "multi_node_examples": ["给小红转200，再给小明转300"],
                    "provides_context_keys": ["transfer_result"],
                    "confirm_policy": "multi_node_only",
                    "max_nodes_per_message": 4,
                },
                "resume_policy": "resume_same_task",
            },
            {
                "intent_code": "fallback_general",
                "name": "兜底",
                "description": "未命中时兜底",
                "domain_code": "fallback",
                "domain_name": "兜底",
                "domain_description": "兜底处理",
                "examples": ["随便聊聊"],
                "agent_url": "https://agent.example.com/fallback",
                "is_leaf_intent": True,
                "parent_intent_code": "",
                "status": "inactive",
                "routing_examples": ["不知道怎么分类"],
                "is_fallback": True,
                "dispatch_priority": 1,
                "request_schema": {},
                "field_mapping": {},
                "field_catalog": [],
                "slot_schema": [],
                "graph_build_hints": {},
                "resume_policy": "resume_same_task",
            },
        ]
    }


def _base_intents_payload() -> dict[str, object]:
    payload = _catalog_payload()
    base_items: list[dict[str, object]] = []
    for item in payload["intents"]:
        if not isinstance(item, dict):
            continue
        base_item = dict(item)
        base_item.pop("field_catalog", None)
        base_item.pop("slot_schema", None)
        base_item.pop("graph_build_hints", None)
        base_items.append(base_item)
    return {"intents": base_items}


def _field_catalogs_payload() -> dict[str, object]:
    payload = _catalog_payload()
    return {
        "field_catalogs": {
            item["intent_code"]: item.get("field_catalog", [])
            for item in payload["intents"]
            if isinstance(item, dict)
        }
    }


def _slot_schemas_payload() -> dict[str, object]:
    payload = _catalog_payload()
    return {
        "slot_schemas": {
            item["intent_code"]: item.get("slot_schema", [])
            for item in payload["intents"]
            if isinstance(item, dict)
        }
    }


def _graph_build_hints_payload() -> dict[str, object]:
    payload = _catalog_payload()
    return {
        "graph_build_hints": {
            item["intent_code"]: item.get("graph_build_hints", {})
            for item in payload["intents"]
            if isinstance(item, dict)
        }
    }


def test_file_intent_repository_loads_domains_and_slots(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intent-catalog.json"
    catalog_path.write_text(
        json.dumps(_catalog_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    repository = FileIntentRepository(catalog_path)

    records = repository.list_intents(IntentStatus.ACTIVE)

    assert len(records) == 1
    record = records[0]
    assert record.intent_code == "transfer_money"
    assert record.domain_code == "payment"
    assert record.field_catalog[0].field_code == "amount"
    assert record.slot_schema[0].slot_key == "amount"
    assert record.graph_build_hints.confirm_policy.value == "multi_node_only"


def test_file_intent_repository_loads_split_catalog_files(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intents.json"
    field_catalog_path = tmp_path / "field-catalogs.json"
    slot_schema_path = tmp_path / "slot-schemas.json"
    graph_build_hints_path = tmp_path / "graph-build-hints.json"
    catalog_path.write_text(
        json.dumps(_base_intents_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    field_catalog_path.write_text(
        json.dumps(_field_catalogs_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    slot_schema_path.write_text(
        json.dumps(_slot_schemas_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    graph_build_hints_path.write_text(
        json.dumps(_graph_build_hints_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    repository = FileIntentRepository(
        catalog_path,
        field_catalog_path=field_catalog_path,
        slot_schema_path=slot_schema_path,
        graph_build_hints_path=graph_build_hints_path,
    )

    records = repository.list_intents(IntentStatus.ACTIVE)

    assert len(records) == 1
    record = records[0]
    assert record.intent_code == "transfer_money"
    assert record.field_catalog[0].field_code == "amount"
    assert record.slot_schema[0].slot_key == "amount"
    assert record.graph_build_hints.confirm_policy.value == "multi_node_only"


def test_file_intent_repository_supports_intent_only_base_catalog(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intents.json"
    catalog_path.write_text(
        json.dumps(_base_intents_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    repository = FileIntentRepository(catalog_path)

    records = repository.list_intents(IntentStatus.ACTIVE)

    assert len(records) == 1
    record = records[0]
    assert record.intent_code == "transfer_money"
    assert record.field_catalog == []
    assert record.slot_schema == []
    assert record.graph_build_hints.max_nodes_per_message == 4


def test_file_intent_repository_reloads_file_changes(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intent-catalog.json"
    catalog_path.write_text(
        json.dumps(_catalog_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    repository = FileIntentRepository(catalog_path)

    updated = _catalog_payload()
    updated["intents"][0]["status"] = "inactive"
    catalog_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")

    assert repository.list_intents(IntentStatus.ACTIVE) == []


def test_file_intent_repository_rejects_writes(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intent-catalog.json"
    catalog_path.write_text(
        json.dumps(_catalog_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    repository = FileIntentRepository(catalog_path)
    payload = IntentPayload.model_validate(_catalog_payload()["intents"][0])

    with pytest.raises(IntentRepositoryReadOnlyError):
        repository.create_intent(payload)


def test_file_intent_repository_requires_valid_json_shape(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intent-catalog.json"
    catalog_path.write_text(json.dumps({"unexpected": []}), encoding="utf-8")
    repository = FileIntentRepository(catalog_path)

    with pytest.raises(IntentRepositoryError):
        repository.list_intents()


def test_file_intent_repository_requires_valid_overlay_shape(tmp_path: Path) -> None:
    catalog_path = tmp_path / "intents.json"
    slot_schema_path = tmp_path / "slot-schemas.json"
    catalog_path.write_text(
        json.dumps(_base_intents_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    slot_schema_path.write_text(json.dumps({"slot_schemas": []}), encoding="utf-8")
    repository = FileIntentRepository(
        catalog_path,
        slot_schema_path=slot_schema_path,
    )

    with pytest.raises(IntentRepositoryError):
        repository.list_intents()
