from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from router_core.domain import ChatMessage  # noqa: E402
from router_core.memory_store import LongTermMemoryStore  # noqa: E402


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
