from __future__ import annotations

from types import SimpleNamespace

from router_service.core.shared.domain import ChatMessage  # noqa: E402
from router_service.core.support.memory_store import LongTermMemoryStore  # noqa: E402


def test_long_term_memory_store_promotes_recent_messages_and_slots() -> None:
    store = LongTermMemoryStore()
    session = SimpleNamespace(
        session_id="session_001",
        cust_id="cust_001",
        messages=[
            ChatMessage(role="user", content="帮我查余额"),
            ChatMessage(role="assistant", content="请提供卡号"),
        ],
        tasks=[
            SimpleNamespace(intent_code="query_account_balance", slot_memory={"card_number": "6222021234567890"}),
            SimpleNamespace(intent_code="transfer_money", slot_memory={}),
        ],
    )

    store.promote_session(session)

    recalled = store.recall("cust_001")
    assert "user: 帮我查余额" in recalled
    assert "assistant: 请提供卡号" in recalled
    assert "query_account_balance: card_number=6222021234567890" in recalled
    assert not any(item.startswith("transfer_money:") for item in recalled)


def test_long_term_memory_store_trims_oldest_facts_when_capacity_reached() -> None:
    store = LongTermMemoryStore(fact_limit=3)
    session = SimpleNamespace(
        session_id="session_limit",
        cust_id="cust_limit",
        messages=[ChatMessage(role="user", content=f"message {idx}") for idx in range(4)],
        tasks=[],
    )

    store.promote_session(session)

    recalled = store.recall("cust_limit", limit=10)
    assert len(recalled) == 3
    assert recalled == [
        "user: message 1",
        "user: message 2",
        "user: message 3",
    ]


def test_long_term_memory_store_applies_environment_limit(monkeypatch) -> None:
    monkeypatch.setenv("ROUTER_LONG_TERM_MEMORY_FACT_LIMIT", "2")
    store = LongTermMemoryStore()
    session = SimpleNamespace(
        session_id="session_env",
        cust_id="cust_env",
        messages=[ChatMessage(role="assistant", content=f"reply {idx}") for idx in range(3)],
        tasks=[],
    )

    store.promote_session(session)

    recalled = store.recall("cust_env", limit=10)
    assert len(recalled) == 2
    assert recalled == ["assistant: reply 1", "assistant: reply 2"]
