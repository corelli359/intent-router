from __future__ import annotations

from pathlib import Path

from admin_service.models.intent import IntentPayload as AdminIntentPayload  # noqa: E402
from admin_service.models.intent import IntentStatus as AdminIntentStatus  # noqa: E402
from admin_service.storage.sql_intent_repository import DatabaseIntentRepository as AdminDatabaseIntentRepository  # noqa: E402
from router_service.catalog.sql_intent_repository import DatabaseIntentRepository as RouterDatabaseIntentRepository  # noqa: E402


def _admin_payload(intent_code: str) -> AdminIntentPayload:
    return AdminIntentPayload(
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
        status=AdminIntentStatus.ACTIVE,
        routing_examples=[f"{intent_code} routing"],
        dispatch_priority=100,
        request_schema={"type": "object"},
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
        resume_policy="resume_same_task",
    )


def test_admin_and_router_repositories_share_intent_registry_shape(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'shared-intent-registry.db'}"
    admin_repository = AdminDatabaseIntentRepository(database_url)
    admin_repository.create_intent(_admin_payload("pay_water"))

    router_repository = RouterDatabaseIntentRepository(database_url)
    record = router_repository.get_intent("pay_water")

    assert record.domain_code == "payment"
    assert record.domain_name == "Payment"
    assert record.domain_description == "Payment domain intents"
    assert record.is_leaf_intent is True
    assert record.parent_intent_code == ""
    assert record.routing_examples == ["pay_water routing"]
    assert record.field_catalog[0].field_code == "free_text_input"
    assert record.slot_schema[0].field_code == "free_text_input"


def test_admin_repository_can_write_after_router_repository_creates_shared_table(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'shared-intent-registry-router-first.db'}"
    router_repository = RouterDatabaseIntentRepository(database_url)
    admin_repository = AdminDatabaseIntentRepository(database_url)

    admin_repository.create_intent(_admin_payload("pay_electricity"))
    record = router_repository.get_intent("pay_electricity")

    assert record.domain_code == "payment"
    assert record.routing_examples == ["pay_electricity routing"]
