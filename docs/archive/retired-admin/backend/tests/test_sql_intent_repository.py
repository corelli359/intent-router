from __future__ import annotations

import sqlite3
from pathlib import Path

from admin_service.models.intent import IntentPayload, IntentStatus  # noqa: E402
from admin_service.storage.sql_intent_repository import DatabaseIntentRepository  # noqa: E402


def _payload(intent_code: str, *, status: IntentStatus = IntentStatus.INACTIVE, is_fallback: bool = False) -> IntentPayload:
    return IntentPayload(
        intent_code=intent_code,
        name=f"{intent_code} name",
        description=f"{intent_code} description",
        domain_code="finance",
        domain_name="Finance",
        domain_description="Financial domain intents",
        is_leaf_intent=True,
        parent_intent_code="",
        examples=[f"{intent_code} example"],
        agent_url=f"https://agent.example.com/{intent_code}",
        status=status,
        is_fallback=is_fallback,
        dispatch_priority=100,
        request_schema={"type": "object", "required": ["input"]},
        field_mapping={"input": "$message.current"},
        field_catalog=[
            {
                "field_code": "free_text_input",
                "label": "输入文本",
                "semantic_definition": "当前意图接收的一段自由文本输入",
                "value_type": "string",
                "examples": ["foo"],
            }
        ],
        slot_schema=[
            {
                "slot_key": "input",
                "field_code": "free_text_input",
                "role": "primary_input",
                "label": "输入",
                "description": "必填输入参数",
                "semantic_definition": "当前意图的主输入文本",
                "value_type": "string",
                "required": True,
                "aliases": ["输入"],
                "examples": ["foo"],
            }
        ],
        routing_examples=[f"{intent_code} routing"],
        resume_policy="resume_same_task",
    )


def test_database_repository_persists_records_across_instances(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'intent-router.db'}"
    repository = DatabaseIntentRepository(database_url)
    repository.create_intent(_payload("query_order_status", status=IntentStatus.ACTIVE))
    repository.create_intent(_payload("fallback_general", status=IntentStatus.ACTIVE, is_fallback=True))

    reloaded = DatabaseIntentRepository(database_url)
    all_intents = reloaded.list_intents()
    active_intents = reloaded.list_intents(IntentStatus.ACTIVE)

    assert [intent.intent_code for intent in all_intents] == ["query_order_status", "fallback_general"]
    assert [intent.intent_code for intent in active_intents] == ["query_order_status", "fallback_general"]
    assert reloaded.get_intent("fallback_general").is_fallback is True
    assert reloaded.get_intent("query_order_status").field_catalog[0].field_code == "free_text_input"
    assert reloaded.get_intent("query_order_status").slot_schema[0].slot_key == "input"
    assert reloaded.get_intent("query_order_status").slot_schema[0].field_code == "free_text_input"
    assert reloaded.get_intent("query_order_status").slot_schema[0].role == "primary_input"
    assert reloaded.get_intent("query_order_status").domain_code == "finance"
    assert reloaded.get_intent("query_order_status").domain_name == "Finance"
    assert reloaded.get_intent("query_order_status").routing_examples == ["query_order_status routing"]


def test_database_repository_auto_adds_v21_columns_for_legacy_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-intent-router.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE intent_registry (
                intent_code VARCHAR(128) PRIMARY KEY,
                name VARCHAR(256) NOT NULL,
                description TEXT NOT NULL,
                examples_json TEXT NOT NULL DEFAULT '[]',
                agent_url VARCHAR(2048) NOT NULL,
                status VARCHAR(32) NOT NULL,
                is_fallback BOOLEAN NOT NULL DEFAULT 0,
                dispatch_priority INTEGER NOT NULL DEFAULT 100,
                request_schema_json TEXT NOT NULL DEFAULT '{}',
                field_mapping_json TEXT NOT NULL DEFAULT '{}',
                resume_policy VARCHAR(128) NOT NULL DEFAULT 'resume_same_task',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO intent_registry (
                intent_code, name, description, examples_json, agent_url, status, is_fallback,
                dispatch_priority, request_schema_json, field_mapping_json, resume_policy, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                "transfer_money",
                "transfer_money name",
                "transfer_money description",
                '["transfer example"]',
                "https://agent.example.com/transfer_money",
                "active",
                0,
                100,
                '{"type":"object"}',
                '{"input":"$message.current"}',
                "resume_same_task",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    repository = DatabaseIntentRepository(f"sqlite:///{db_path}")
    record = repository.get_intent("transfer_money")

    assert record.slot_schema == []
    assert record.field_catalog == []
    assert record.domain_code == ""
    assert record.domain_name == ""
    assert record.domain_description == ""
    assert record.is_leaf_intent is True
    assert record.parent_intent_code == ""
    assert record.routing_examples == []
