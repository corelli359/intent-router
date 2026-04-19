from __future__ import annotations

from types import SimpleNamespace

from router_service.core.shared.domain import ChatMessage  # noqa: E402
from router_service.core.shared.domain import utc_now  # noqa: E402
from router_service.core.shared.graph_domain import BusinessMemoryDigest  # noqa: E402
from router_service.core.support.memory_store import (  # noqa: E402
    InProcessMemoryRuntime,
    SessionMemoryDumpRequest,
    SessionMemoryMessageRecord,
    SessionMemoryQuery,
    SessionMemoryRememberRequest,
    SessionMemoryTaskRecord,
)


def _seed_digest(
    *,
    business_id: str = "biz-001",
    intent_codes: list[str] | None = None,
    summary: str = "handover",
    slot_memory: dict[str, object] | None = None,
) -> BusinessMemoryDigest:
    return BusinessMemoryDigest(
        business_id=business_id,
        graph_id=f"graph-{business_id}",
        intent_codes=intent_codes or ["transfer_money"],
        status="completed",
        ishandover=True,
        summary=summary,
        slot_memory=slot_memory or {"amount": "100"},
        created_at=utc_now(),
    )


def test_in_process_memory_runtime_ensures_and_gets_cached_session_memory() -> None:
    runtime = InProcessMemoryRuntime()
    runtime.promote_session(
        SimpleNamespace(
            session_id="seed-1",
            cust_id="cust-001",
            messages=[
                ChatMessage(role="user", content="第一条"),
                ChatMessage(role="assistant", content="第二条"),
            ],
            tasks=[],
            shared_slot_memory={},
            business_memory_digests=[],
        )
    )

    warmed = runtime.ensure_session_memory(
        SessionMemoryQuery(
            session_id="session-001",
            cust_id="cust-001",
            recall_limit=10,
        )
    )
    runtime.promote_session(
        SimpleNamespace(
            session_id="seed-2",
            cust_id="cust-001",
            messages=[ChatMessage(role="user", content="第三条")],
            tasks=[],
            shared_slot_memory={},
            business_memory_digests=[],
        )
    )

    cached = runtime.get_session_memory(
        SessionMemoryQuery(
            session_id="session-001",
            cust_id="cust-001",
            recall_limit=10,
        )
    )

    assert warmed.long_term_memory == ["user: 第一条", "assistant: 第二条"]
    assert cached.long_term_memory == warmed.long_term_memory
    assert "user: 第三条" not in cached.long_term_memory
    assert cached.shared_slot_memory == {}
    assert cached.business_memory_digests == []


def test_in_process_memory_runtime_remember_business_updates_short_term_snapshot() -> None:
    runtime = InProcessMemoryRuntime()
    runtime.ensure_session_memory(
        SessionMemoryQuery(
            session_id="session-remember",
            cust_id="cust-remember",
            recall_limit=5,
        )
    )
    digest = _seed_digest(slot_memory={"amount": "100"})
    shared_slot_memory = {"account_id": "ACC-001"}

    remembered = runtime.remember_business(
        SessionMemoryRememberRequest(
            session_id="session-remember",
            cust_id="cust-remember",
            digest=digest,
            shared_slot_memory=shared_slot_memory,
        )
    )
    digest.slot_memory["amount"] = "999"
    shared_slot_memory["account_id"] = "ACC-999"

    cached = runtime.get_session_memory(
        SessionMemoryQuery(
            session_id="session-remember",
            cust_id="cust-remember",
            recall_limit=5,
        )
    )

    assert remembered.shared_slot_memory == {"account_id": "ACC-001"}
    assert len(remembered.business_memory_digests) == 1
    assert remembered.business_memory_digests[0].slot_memory == {"amount": "100"}
    assert cached.shared_slot_memory == {"account_id": "ACC-001"}
    assert cached.business_memory_digests[0].slot_memory == {"amount": "100"}


def test_in_process_memory_runtime_expires_session_by_promoting_and_clearing_snapshot() -> None:
    runtime = InProcessMemoryRuntime()
    runtime.remember_business(
        session_id="session-expire",
        cust_id="cust-expire",
        digest=_seed_digest(),
        shared_slot_memory={"account_id": "ACC-001"},
    )
    dump = SessionMemoryDumpRequest(
        session_id="session-expire",
        cust_id="cust-expire",
        messages=[SessionMemoryMessageRecord(role="user", content="查询转账记录")],
        tasks=[SessionMemoryTaskRecord(intent_code="transfer_money", slot_memory={"amount": "100"})],
        shared_slot_memory={"account_id": "ACC-001"},
        business_memory_digests=[_seed_digest()],
        reason="expired",
    )

    runtime.expire_session(dump)

    recalled = runtime.recall("cust-expire", limit=10)
    refreshed = runtime.get_session_memory(
        SessionMemoryQuery(
            session_id="session-expire",
            cust_id="cust-expire",
            recall_limit=10,
        )
    )

    assert "user: 查询转账记录" in recalled
    assert "transfer_money: amount=100" in recalled
    assert "shared_slots: account_id=ACC-001" in recalled
    assert "transfer_money: amount=100" in refreshed.long_term_memory
    assert refreshed.shared_slot_memory == {}
    assert refreshed.business_memory_digests == []


def test_in_process_memory_runtime_keeps_legacy_keyword_call_compatibility() -> None:
    runtime = InProcessMemoryRuntime()

    snapshot = runtime.ensure_session_memory(
        session_id="session-legacy",
        cust_id="cust-legacy",
        recall_limit=3,
    )

    assert snapshot.session_id == "session-legacy"
