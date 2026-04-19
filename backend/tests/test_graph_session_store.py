from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.shared.graph_domain import BusinessMemoryDigest
from router_service.core.support.memory_store import (
    LongTermMemoryStore,
    SessionMemoryDumpRequest,
    SessionMemoryQuery,
)


class SpyMemoryRuntime:
    """Track graph session store calls against the memory runtime boundary."""

    def __init__(self) -> None:
        self.long_term_memory = LongTermMemoryStore()
        self.get_calls: list[SessionMemoryQuery] = []
        self.ensure_calls: list[SessionMemoryQuery] = []
        self.remember_calls: list[SessionMemoryRememberRequest] = []
        self.expire_calls: list[SessionMemoryDumpRequest] = []

    def get_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ):
        """Record session snapshot reads and return a lightweight snapshot view."""
        query = request or SessionMemoryQuery(
            session_id=session_id or "",
            cust_id=cust_id or "",
            recall_limit=recall_limit or 0,
        )
        self.get_calls.append(query)
        return SimpleNamespace(
            session_id=query.session_id,
            cust_id=query.cust_id,
            long_term_memory=["snapshot"],
            shared_slot_memory={},
            business_memory_digests=[],
        )

    def ensure_session_memory(
        self,
        request: SessionMemoryQuery | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        recall_limit: int | None = None,
    ):
        """Record session warmup requests and return a lightweight snapshot view."""
        query = request or SessionMemoryQuery(
            session_id=session_id or "",
            cust_id=cust_id or "",
            recall_limit=recall_limit or 0,
        )
        self.ensure_calls.append(query)
        return SimpleNamespace(
            session_id=query.session_id,
            cust_id=query.cust_id,
            long_term_memory=[],
            shared_slot_memory={},
            business_memory_digests=[],
        )

    def remember_business(
        self,
        request: SessionMemoryRememberRequest | None = None,
        *,
        session_id: str | None = None,
        cust_id: str | None = None,
        digest: BusinessMemoryDigest | None = None,
        shared_slot_memory: dict[str, object] | None = None,
    ):
        """Record business handover writes and return a lightweight snapshot view."""
        remember_request = request or SessionMemoryRememberRequest(
            session_id=session_id or "",
            cust_id=cust_id or "",
            digest=digest,
            shared_slot_memory=dict(shared_slot_memory or {}),
        )
        self.remember_calls.append(remember_request)
        return SimpleNamespace(
            session_id=remember_request.session_id,
            cust_id=remember_request.cust_id,
            long_term_memory=[],
            shared_slot_memory=remember_request.shared_slot_memory,
            business_memory_digests=[remember_request.digest],
        )

    def expire_session(self, dump_or_session, *, reason: str = "expired") -> None:
        """Record expiry calls and reuse long-term promotion for compatibility."""
        assert isinstance(dump_or_session, SessionMemoryDumpRequest)
        self.expire_calls.append(dump_or_session)
        self.long_term_memory.promote_dump(dump_or_session)


def test_ensure_session_memory_delegates_to_memory_runtime() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(
        long_term_memory=runtime.long_term_memory,
        memory_runtime=runtime,
        memory_recall_limit=7,
    )
    session = store.create(cust_id="cust-0", session_id="warm")

    snapshot = store.ensure_session_memory(session)

    assert snapshot.session_id == "warm"
    assert snapshot.long_term_memory == []
    assert session.memory_warmed is True
    assert runtime.ensure_calls == [SessionMemoryQuery(session_id="warm", cust_id="cust-0", recall_limit=7)]
    assert runtime.get_calls == []


def test_get_session_memory_warms_once_then_uses_live_session_cache() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(
        long_term_memory=runtime.long_term_memory,
        memory_runtime=runtime,
        memory_recall_limit=9,
    )
    session = store.create(cust_id="cust-0", session_id="read")

    first_snapshot = store.get_session_memory(session)
    session.shared_slot_memory["city"] = "Shanghai"
    second_snapshot = store.get_session_memory(session)

    assert first_snapshot.long_term_memory == []
    assert second_snapshot.long_term_memory == []
    assert second_snapshot.shared_slot_memory == {"city": "Shanghai"}
    assert runtime.ensure_calls == [SessionMemoryQuery(session_id="read", cust_id="cust-0", recall_limit=9)]
    assert runtime.get_calls == []


def test_remember_business_handover_returns_live_session_memory_snapshot() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(long_term_memory=runtime.long_term_memory, memory_runtime=runtime)
    session = store.create(cust_id="cust-1", session_id="handover")
    session.shared_slot_memory = {"city": "Shanghai"}
    digest = BusinessMemoryDigest(
        business_id="biz-1",
        graph_id="graph-1",
        intent_codes=["intent.alpha"],
        status="completed",
        ishandover=True,
        summary="done",
        slot_memory={"city": "Shanghai"},
        created_at=session.created_at,
    )
    session.business_memory_digests.append(digest)

    snapshot = store.remember_business_handover(session, digest)

    assert snapshot.business_memory_digests == [digest]
    assert snapshot.shared_slot_memory == {"city": "Shanghai"}
    assert runtime.remember_calls == []


def test_get_or_create_recreates_expired_session_via_memory_runtime() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(long_term_memory=runtime.long_term_memory, memory_runtime=runtime)
    expired = store.create(cust_id="cust-1", session_id="expired")
    expired.expires_at = expired.created_at - timedelta(seconds=1)

    recreated = store.get_or_create("expired", cust_id="cust-1")

    assert recreated.session_id == "expired"
    assert recreated.cust_id == "cust-1"
    assert recreated is not expired
    assert [dump.session_id for dump in runtime.expire_calls] == ["expired"]
    assert runtime.expire_calls[0].reason == "expired"
    assert store.get("expired") is recreated


def test_get_or_create_recreates_customer_mismatch_via_memory_runtime() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(long_term_memory=runtime.long_term_memory, memory_runtime=runtime)
    original = store.create(cust_id="cust-1", session_id="shared-session")

    recreated = store.get_or_create("shared-session", cust_id="cust-2")

    assert recreated.session_id == "shared-session"
    assert recreated.cust_id == "cust-2"
    assert recreated is not original
    assert [dump.session_id for dump in runtime.expire_calls] == ["shared-session"]
    assert runtime.expire_calls[0].reason == "cust_mismatch"
    assert store.get("shared-session") is recreated


def test_purge_expired_expires_and_removes_expired_session() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(long_term_memory=runtime.long_term_memory, memory_runtime=runtime)
    active = store.create(cust_id="cust-1", session_id="active")
    expired = store.create(cust_id="cust-1", session_id="expired")
    expired.expires_at = expired.created_at - timedelta(seconds=1)
    store.note_session_expiry(expired)

    removed = store.purge_expired()

    assert removed == ["expired"]
    assert [dump.session_id for dump in runtime.expire_calls] == ["expired"]
    assert runtime.expire_calls[0].reason == "expired"
    assert store.get(active.session_id) is active
    with pytest.raises(KeyError):
        store.get(expired.session_id)


def test_purge_expired_no_action_when_sessions_are_fresh() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(long_term_memory=runtime.long_term_memory, memory_runtime=runtime)
    store.create(cust_id="cust-2", session_id="fresh")

    removed = store.purge_expired()

    assert removed == []
    assert runtime.expire_calls == []


def test_purge_expired_skips_session_when_expiry_was_extended() -> None:
    runtime = SpyMemoryRuntime()
    store = GraphSessionStore(long_term_memory=runtime.long_term_memory, memory_runtime=runtime)
    session = store.create(cust_id="cust-3", session_id="extended")
    original_expiry = session.expires_at
    session.expires_at = original_expiry + timedelta(minutes=10)

    removed = store.purge_expired(now=original_expiry + timedelta(seconds=1))

    assert removed == []
    assert runtime.expire_calls == []
    assert store.get(session.session_id) is session

    removed = store.purge_expired(now=session.expires_at + timedelta(seconds=1))

    assert removed == ["extended"]
    assert [dump.session_id for dump in runtime.expire_calls] == ["extended"]
    assert runtime.expire_calls[0].reason == "expired"
    with pytest.raises(KeyError):
        store.get(session.session_id)


def test_session_lock_serializes_same_session_id() -> None:
    store = GraphSessionStore()
    events: list[str] = []

    async def first() -> None:
        async with store.session_lock("session-lock"):
            events.append("first-start")
            await asyncio.sleep(0.01)
            events.append("first-end")

    async def second() -> None:
        await asyncio.sleep(0)
        async with store.session_lock("session-lock"):
            events.append("second-start")
            events.append("second-end")

    async def run() -> None:
        await asyncio.gather(first(), second())

    asyncio.run(run())

    assert events == ["first-start", "first-end", "second-start", "second-end"]
