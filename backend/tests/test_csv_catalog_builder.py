from __future__ import annotations

import csv
import json
from pathlib import Path

from router_service.catalog.csv_catalog_builder import (
    DEFAULT_FALLBACK_AGENT_URL,
    build_csv_catalog_payloads,
    parse_examples,
    unique_intent_code,
)


def test_unique_intent_code_appends_row_num_for_duplicates() -> None:
    assert unique_intent_code(raw_code="AG_MENU", row_num="6", duplicate_count=2) == "AG_MENU_6"
    assert unique_intent_code(raw_code="AG_TRANS", row_num="20", duplicate_count=1) == "AG_TRANS"


def test_parse_examples_splits_screenshot_column() -> None:
    assert parse_examples("发起转账：我要转账：立即转账") == [
        "发起转账",
        "我要转账",
        "立即转账",
    ]


def test_build_csv_catalog_payloads_preserves_transfer_slots_and_generates_recognition_only_intents(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "intent_table.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["row_num", "intent_code", "name", "category", "description", "examples"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "row_num": "6",
                "intent_code": "AG_MENU",
                "name": "生活服务",
                "category": "生活服务",
                "description": "日常生活相关的非金融服务入口。",
                "examples": "生活服务：掌银本地生活",
            }
        )
        writer.writerow(
            {
                "row_num": "8",
                "intent_code": "AG_MENU",
                "name": "办理各种付款业务",
                "category": "付款业务",
                "description": "各类支付付款功能办理。",
                "examples": "办理付款业务：扫码支付",
            }
        )
        writer.writerow(
            {
                "row_num": "20",
                "intent_code": "AG_TRANS",
                "name": "立即发起一笔转账交易",
                "category": "转账服务",
                "description": "实时转账交易执行。",
                "examples": "发起转账：我要转账：立即转账",
            }
        )

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    transfer_payload = {
        "intent_code": "transfer_money",
        "name": "转账",
        "description": "旧转账定义",
        "domain_code": "transfer",
        "domain_name": "转账",
        "domain_description": "旧转账域",
        "examples": ["给小红转200"],
        "routing_examples": ["转账"],
        "agent_url": "http://intent-appointment-agent.intent.svc.cluster.local:8000/api/agent/run",
        "is_leaf_intent": True,
        "parent_intent_code": "",
        "status": "active",
        "dispatch_priority": 95,
        "request_schema": {"type": "object", "required": ["sessionId", "taskId", "input"]},
        "field_mapping": {"input": "$message.current"},
        "field_catalog": [
            {
                "field_code": "payee_name",
                "label": "收款人姓名",
                "semantic_definition": "当前转账对应的收款人姓名。",
                "value_type": "string",
            }
        ],
        "slot_schema": [
            {
                "slot_key": "payee_name",
                "field_code": "payee_name",
                "role": "payee_name",
                "label": "收款人姓名",
                "description": "收款人姓名。",
                "semantic_definition": "当前转账的收款人姓名。",
                "value_type": "string",
                "required": True,
            }
        ],
        "graph_build_hints": {},
        "resume_policy": "resume_same_task",
    }
    (catalog_dir / "intents.json").write_text(
        json.dumps(
            {
                "intents": [
                    {
                        key: value
                        for key, value in transfer_payload.items()
                        if key not in {"field_catalog", "slot_schema", "graph_build_hints"}
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (catalog_dir / "field-catalogs.json").write_text(
        json.dumps({"field_catalogs": {"transfer_money": transfer_payload["field_catalog"]}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (catalog_dir / "slot-schemas.json").write_text(
        json.dumps({"slot_schemas": {"transfer_money": transfer_payload["slot_schema"]}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (catalog_dir / "graph-build-hints.json").write_text(
        json.dumps({"graph_build_hints": {"transfer_money": transfer_payload["graph_build_hints"]}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    payloads = build_csv_catalog_payloads(
        csv_path=csv_path,
        existing_catalog_dir=catalog_dir,
    )
    payloads_by_code = {item["intent_code"]: item for item in payloads}

    assert set(payloads_by_code) == {"AG_MENU_6", "AG_MENU_8", "AG_TRANS"}

    transfer = payloads_by_code["AG_TRANS"]
    assert transfer["name"] == "立即发起一笔转账交易"
    assert transfer["agent_url"] == transfer_payload["agent_url"]
    assert transfer["graph_build_hints"]["provides_context_keys"] == ["amount", "business_status"]
    assert [field["field_code"] for field in transfer["field_catalog"]] == ["payee_name"]
    assert transfer["field_catalog"][0]["label"] == "收款人姓名"
    assert [slot["slot_key"] for slot in transfer["slot_schema"]] == ["payee_name"]
    assert transfer["slot_schema"][0]["field_code"] == "payee_name"
    assert transfer["slot_schema"][0]["required"] is True

    generic = payloads_by_code["AG_MENU_6"]
    assert generic["slot_schema"] == []
    assert generic["field_catalog"] == []
    assert generic["agent_url"] == DEFAULT_FALLBACK_AGENT_URL
    assert generic["graph_build_hints"]["confirm_policy"] == "always"
