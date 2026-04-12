from __future__ import annotations

import sqlite3
from pathlib import Path

from router_service.catalog.sql_intent_repository import DatabaseIntentRepository  # noqa: E402
from router_service.models.intent import IntentPayload, IntentStatus  # noqa: E402


def _payload(
    intent_code: str,
    *,
    status: IntentStatus = IntentStatus.INACTIVE,
    is_fallback: bool = False,
) -> IntentPayload:
    return IntentPayload(
        intent_code=intent_code,
        name=f"{intent_code} name",
        description=f"{intent_code} description",
        domain_code="payment",
        domain_name="Payment",
        domain_description="Payment domain intents",
        examples=[f"{intent_code} example"],
        agent_url=f"https://agent.example.com/{intent_code}",
        is_leaf_intent=True,
        parent_intent_code="",
        status=status,
        routing_examples=[f"{intent_code} routing"],
        is_fallback=is_fallback,
        dispatch_priority=100,
        request_schema={"type": "object", "required": ["input"]},
        field_mapping={"input": "$message.current"},
        field_catalog=[
            {
                "field_code": "free_text_input",
                "label": "Input",
                "semantic_definition": "Free text input for this intent",
                "value_type": "string",
                "examples": ["foo"],
            }
        ],
        slot_schema=[
            {
                "slot_key": "input",
                "field_code": "free_text_input",
                "role": "primary_input",
                "label": "Input",
                "description": "Required input field",
                "semantic_definition": "Primary input text for this intent",
                "value_type": "string",
                "required": True,
                "aliases": ["input"],
                "examples": ["foo"],
            }
        ],
        graph_build_hints={
            "intent_scope_rule": "One message maps to one node by default.",
            "planner_notes": "Split only when the user expresses independent actions.",
        },
        resume_policy="resume_same_task",
    )


def test_router_database_repository_persists_hierarchical_fields(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'router-intent.db'}"
    repository = DatabaseIntentRepository(database_url)
    repository.create_intent(_payload("pay_electricity", status=IntentStatus.ACTIVE))
    repository.create_intent(_payload("fallback_general", status=IntentStatus.ACTIVE, is_fallback=True))

    reloaded = DatabaseIntentRepository(database_url)
    record = reloaded.get_intent("pay_electricity")

    assert record.domain_code == "payment"
    assert record.domain_name == "Payment"
    assert record.domain_description == "Payment domain intents"
    assert record.is_leaf_intent is True
    assert record.parent_intent_code == ""
    assert record.routing_examples == ["pay_electricity routing"]
    assert record.field_catalog[0].field_code == "free_text_input"
    assert record.slot_schema[0].field_code == "free_text_input"


def test_router_database_repository_auto_adds_hierarchical_columns_for_legacy_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-router-intent.db"
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

    assert record.domain_code == ""
    assert record.domain_name == ""
    assert record.domain_description == ""
    assert record.is_leaf_intent is True
    assert record.parent_intent_code == ""
    assert record.routing_examples == []
