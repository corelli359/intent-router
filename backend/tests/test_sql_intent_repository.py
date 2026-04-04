from __future__ import annotations

import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from models.intent import IntentPayload, IntentStatus  # noqa: E402
from persistence.sql_intent_repository import DatabaseIntentRepository  # noqa: E402


def _payload(intent_code: str, *, status: IntentStatus = IntentStatus.INACTIVE, is_fallback: bool = False) -> IntentPayload:
    return IntentPayload(
        intent_code=intent_code,
        name=f"{intent_code} name",
        description=f"{intent_code} description",
        examples=[f"{intent_code} example"],
        agent_url=f"https://agent.example.com/{intent_code}",
        status=status,
        is_fallback=is_fallback,
        dispatch_priority=100,
        request_schema={"type": "object", "required": ["input"]},
        field_mapping={"input": "$message.current"},
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
