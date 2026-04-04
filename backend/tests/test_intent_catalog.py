from __future__ import annotations

import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from models.intent import IntentPayload, IntentStatus  # noqa: E402
from persistence.in_memory_intent_repository import InMemoryIntentRepository  # noqa: E402
from router_core.intent_catalog import RepositoryIntentCatalog  # noqa: E402


def _payload(*, intent_code: str, status: IntentStatus) -> IntentPayload:
    return IntentPayload(
        intent_code=intent_code,
        name=intent_code,
        description=f"description for {intent_code}",
        examples=[f"example for {intent_code}"],
        agent_url=f"http://agent.example.com/{intent_code}",
        status=status,
        dispatch_priority=100,
        request_schema={"type": "object"},
        field_mapping={"input": "$message.current"},
        resume_policy="resume_same_task",
    )


def test_catalog_only_exposes_active_intents_after_refresh_interval() -> None:
    now = [100.0]

    def clock() -> float:
        return now[0]

    repository = InMemoryIntentRepository()
    repository.create_intent(_payload(intent_code="query_order_status", status=IntentStatus.INACTIVE))
    catalog = RepositoryIntentCatalog(repository, refresh_interval_seconds=5.0, clock=clock)

    assert catalog.list_active() == []

    repository.update_intent(
        "query_order_status",
        _payload(intent_code="query_order_status", status=IntentStatus.ACTIVE),
    )

    assert catalog.list_active() == []

    now[0] += 5.1
    active_intents = catalog.list_active()
    assert [intent.intent_code for intent in active_intents] == ["query_order_status"]
    assert catalog.priorities() == {"query_order_status": 100}


def test_catalog_excludes_fallback_from_recognition_but_keeps_it_available_for_dispatch() -> None:
    repository = InMemoryIntentRepository()
    repository.create_intent(_payload(intent_code="query_order_status", status=IntentStatus.ACTIVE))
    repository.create_intent(
        _payload(intent_code="fallback_general", status=IntentStatus.ACTIVE).model_copy(
            update={"is_fallback": True, "dispatch_priority": 1}
        )
    )

    catalog = RepositoryIntentCatalog(repository, refresh_interval_seconds=5.0)

    assert [intent.intent_code for intent in catalog.list_active()] == ["query_order_status"]
    assert catalog.get_fallback_intent() is not None
    assert catalog.get_fallback_intent().intent_code == "fallback_general"
    assert catalog.priorities()["fallback_general"] == 1
