from __future__ import annotations

from router_service.models.intent import IntentPayload, IntentStatus  # noqa: E402
from router_service.catalog.in_memory_intent_repository import InMemoryIntentRepository  # noqa: E402
from router_service.core.support.intent_catalog import RepositoryIntentCatalog  # noqa: E402


def _payload(
    *,
    intent_code: str,
    status: IntentStatus,
    domain_code: str = "",
    domain_name: str = "",
    domain_description: str = "",
    routing_examples: list[str] | None = None,
) -> IntentPayload:
    return IntentPayload(
        intent_code=intent_code,
        name=intent_code,
        description=f"description for {intent_code}",
        domain_code=domain_code,
        domain_name=domain_name,
        domain_description=domain_description,
        examples=[f"example for {intent_code}"],
        agent_url=f"http://agent.example.com/{intent_code}",
        routing_examples=routing_examples or [],
        status=status,
        dispatch_priority=100,
        request_schema={"type": "object"},
        field_mapping={"input": "$message.current"},
        slot_schema=[
            {
                "slot_key": "input",
                "label": "输入",
                "description": "示例输入字段",
                "value_type": "string",
                "required": True,
            }
        ],
        graph_build_hints={
            "intent_scope_rule": "单条消息默认只映射一个节点。",
            "planner_notes": "只有明确并列动作才允许拆分。",
        },
        resume_policy="resume_same_task",
    )


def test_catalog_keeps_cached_snapshot_until_refresh_now() -> None:
    repository = InMemoryIntentRepository()
    repository.create_intent(_payload(intent_code="query_order_status", status=IntentStatus.INACTIVE))
    catalog = RepositoryIntentCatalog(repository)

    assert catalog.list_active() == []

    repository.update_intent(
        "query_order_status",
        _payload(intent_code="query_order_status", status=IntentStatus.ACTIVE),
    )

    catalog.refresh_now()
    assert [intent.intent_code for intent in catalog.list_active()] == ["query_order_status"]
    assert catalog.list_active()[0].slot_schema[0].slot_key == "input"

    repository.update_intent(
        "query_order_status",
        _payload(intent_code="query_order_status", status=IntentStatus.INACTIVE),
    )

    assert [intent.intent_code for intent in catalog.list_active()] == ["query_order_status"]
    assert catalog.priorities() == {"query_order_status": 100}

    assert [intent.intent_code for intent in catalog.list_active()] == ["query_order_status"]

    catalog.refresh_now()
    assert catalog.list_active() == []
    assert catalog.priorities() == {}


def test_catalog_excludes_fallback_from_recognition_but_keeps_it_available_for_dispatch() -> None:
    repository = InMemoryIntentRepository()
    repository.create_intent(_payload(intent_code="query_order_status", status=IntentStatus.ACTIVE))
    repository.create_intent(
        _payload(intent_code="fallback_general", status=IntentStatus.ACTIVE).model_copy(
            update={"is_fallback": True, "dispatch_priority": 1}
        )
    )

    catalog = RepositoryIntentCatalog(repository)
    catalog.refresh_now()

    assert [intent.intent_code for intent in catalog.list_active()] == ["query_order_status"]
    assert catalog.get_fallback_intent() is not None
    assert catalog.get_fallback_intent().intent_code == "fallback_general"
    assert catalog.priorities()["fallback_general"] == 1


def test_catalog_reads_cached_snapshot_without_sync_refresh() -> None:
    repository = InMemoryIntentRepository()
    repository.create_intent(_payload(intent_code="transfer_money", status=IntentStatus.ACTIVE))
    catalog = RepositoryIntentCatalog(repository)
    catalog.refresh_now()

    repository.update_intent(
        "transfer_money",
        _payload(intent_code="transfer_money", status=IntentStatus.INACTIVE),
    )

    assert [intent.intent_code for intent in catalog.list_active()] == ["transfer_money"]
    catalog.refresh_now()
    assert catalog.list_active() == []


def test_catalog_groups_leaf_intents_into_domains() -> None:
    repository = InMemoryIntentRepository()
    repository.create_intent(
        _payload(
            intent_code="pay_electricity",
            status=IntentStatus.ACTIVE,
            domain_code="payment",
            domain_name="缴费",
            routing_examples=["交电费"],
        )
    )
    repository.create_intent(
        _payload(
            intent_code="pay_gas",
            status=IntentStatus.ACTIVE,
            domain_code="payment",
            domain_name="缴费",
            routing_examples=["交燃气费"],
        )
    )
    repository.create_intent(
        _payload(
            intent_code="transfer_money",
            status=IntentStatus.ACTIVE,
            domain_code="transfer",
            domain_name="转账",
            routing_examples=["转账"],
        )
    )
    catalog = RepositoryIntentCatalog(repository)
    catalog.refresh_now()
    domains = {domain.domain_code: domain for domain in catalog.list_active_domains()}
    assert set(domains) == {"payment", "transfer"}
    payment_domain = domains["payment"]
    assert payment_domain.domain_name == "缴费"
    assert len(payment_domain.leaf_intents) == 2
    assert "交电费" in payment_domain.routing_examples
    assert payment_domain.leaf_intents[0].domain_code == "payment"
    assert payment_domain.leaf_intents[0].is_leaf_intent is True
    assert catalog.list_active_leaf_intents("payment")
    assert not catalog.list_active_leaf_intents("missing")


def test_catalog_excludes_non_leaf_intents_from_routable_snapshot() -> None:
    repository = InMemoryIntentRepository()
    repository.create_intent(
        _payload(
            intent_code="payment_domain",
            status=IntentStatus.ACTIVE,
            domain_code="payment",
            domain_name="缴费",
        ).model_copy(update={"is_leaf_intent": False})
    )
    repository.create_intent(
        _payload(
            intent_code="pay_electricity",
            status=IntentStatus.ACTIVE,
            domain_code="payment",
            domain_name="缴费",
            routing_examples=["交电费"],
        )
    )
    catalog = RepositoryIntentCatalog(repository)
    catalog.refresh_now()

    assert [intent.intent_code for intent in catalog.list_active()] == ["pay_electricity"]
    assert [intent.intent_code for intent in catalog.list_active_leaf_intents("payment")] == ["pay_electricity"]
