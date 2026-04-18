from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.support.memory_store import LongTermMemoryStore


class SpyMemory(LongTermMemoryStore):
    """Track which sessions were promoted during purge cycles."""

    def __init__(self) -> None:
        super().__init__()
        self.promoted_sessions: list[str] = []

    def promote_session(self, session) -> None:
        """Wrap the base promotion to record the session id for assertions."""
        super().promote_session(session)
        self.promoted_sessions.append(session.session_id)


def test_purge_expired_promotes_and_removes_expired_session() -> None:
    memory = SpyMemory()
    store = GraphSessionStore(long_term_memory=memory)
    active = store.create(cust_id="cust-1", session_id="active")
    expired = store.create(cust_id="cust-1", session_id="expired")
    expired.expires_at = expired.created_at - timedelta(seconds=1)

    removed = store.purge_expired()

    assert removed == ["expired"]
    assert memory.promoted_sessions == ["expired"]
    assert store.get(active.session_id) is active
    with pytest.raises(KeyError):
        store.get(expired.session_id)


def test_purge_expired_no_action_when_sessions_are_fresh() -> None:
    memory = SpyMemory()
    store = GraphSessionStore(long_term_memory=memory)
    store.create(cust_id="cust-2", session_id="fresh")

    removed = store.purge_expired()

    assert removed == []
    assert memory.promoted_sessions == []


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
