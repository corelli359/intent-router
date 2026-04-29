from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
import heapq
import threading
from typing import Any
from uuid import uuid4

from router_service.core.shared.domain import utc_now
from router_service.core.shared.graph_domain import GraphSessionState, SessionRuntimeState
from router_service.core.support.memory_store import LongTermMemoryStore


class GraphSessionStore:
    """In-memory session store responsible for graph session lifecycle and expiry handling."""

    def __init__(self, long_term_memory: LongTermMemoryStore | None = None) -> None:
        """Initialize the session store and its long-term memory backend."""
        self._sessions: dict[str, GraphSessionState] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._expiry_heap: list[tuple[datetime, str]] = []
        self._state_lock = threading.RLock()
        self.long_term_memory = long_term_memory or LongTermMemoryStore()

    def create(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        """Create and store a fresh graph session."""
        resolved_session_id = session_id or f"session_graph_{uuid4().hex[:10]}"
        session = GraphSessionState(session_id=resolved_session_id, cust_id=cust_id)
        with self._state_lock:
            return self._store_session(session)

    def get(self, session_id: str) -> GraphSessionState:
        """Return an existing graph session by id."""
        with self._state_lock:
            return self._sessions[session_id]

    @asynccontextmanager
    async def session_lock(self, session_id: str) -> AsyncIterator[None]:
        """Serialize concurrent mutations for one session id."""
        with self._state_lock:
            lock = self._locks[session_id]
        async with lock:
            yield

    @asynccontextmanager
    async def request_scope(
        self,
        session_id: str,
        cust_id: str | None = None,
        *,
        create: bool = True,
    ) -> AsyncIterator[GraphSessionState]:
        """Own one API request's session runtime state and idle-time boundary."""
        async with self.session_lock(session_id):
            if create:
                if cust_id is None:
                    raise ValueError("cust_id is required when creating a router session")
                session = self.get_or_create(session_id, cust_id)
            else:
                session = self.get(session_id)
            session.mark_running()
            try:
                yield session
            finally:
                with self._state_lock:
                    if self._sessions.get(session.session_id) is session:
                        session.mark_idle()
                        self._push_expiry(session)

    def get_or_create(self, session_id: str | None, cust_id: str) -> GraphSessionState:
        """Resolve a session id, recreating expired or customer-mismatched sessions when needed."""
        if session_id is None:
            return self.create(cust_id=cust_id)
        with self._state_lock:
            session = self._sessions.get(session_id)
            if session is None:
                return self._store_session(GraphSessionState(session_id=session_id, cust_id=cust_id))
            if session.cust_id != cust_id:
                return self._store_session(GraphSessionState(session_id=session_id, cust_id=cust_id))
            if session.is_expired():
                self.long_term_memory.promote_session(self._compat_session_view(session))
                return self._store_session(GraphSessionState(session_id=session.session_id, cust_id=session.cust_id))
            return session

    def note_session_expiry(self, session: GraphSessionState) -> None:
        """Track an externally updated session expiry without changing session contents."""
        with self._state_lock:
            if self._sessions.get(session.session_id) is session:
                self._push_expiry(session)

    def _compat_session_view(self, session: GraphSessionState) -> Any:
        """Expose the subset of session fields required by long-term memory promotion."""
        class _Compat:
            """Lightweight memory-promotion view over a graph session."""

            def __init__(self, source: GraphSessionState) -> None:
                """Copy only memory-relevant fields out of the graph session."""
                self.session_id = source.session_id
                self.cust_id = source.cust_id
                self.messages = source.messages
                self.tasks = source.tasks
                self.shared_slot_memory = source.shared_slot_memory
                self.business_memory_digests = source.business_memory_digests

        return _Compat(session)

    def _store_session(self, session: GraphSessionState) -> GraphSessionState:
        """Persist one live session and index its current expiry deadline."""
        self._sessions[session.session_id] = session
        self._push_expiry(session)
        return session

    def _push_expiry(self, session: GraphSessionState) -> None:
        """Record the session's current expiry in the lazy invalidation heap."""
        heapq.heappush(self._expiry_heap, (session.expires_at, session.session_id))

    def purge_expired(self, now: datetime | None = None) -> list[str]:
        """Promote and remove any sessions whose TTL has elapsed."""
        current_time = now or utc_now()
        expired_sessions: list[str] = []
        with self._state_lock:
            while self._expiry_heap:
                tracked_expiry, session_id = self._expiry_heap[0]
                if tracked_expiry > current_time:
                    break
                heapq.heappop(self._expiry_heap)
                session = self._sessions.get(session_id)
                if session is None:
                    continue
                if session.expires_at > tracked_expiry:
                    self._push_expiry(session)
                    continue
                if session.runtime_state == SessionRuntimeState.RUNNING:
                    continue
                if not session.is_expired(now=current_time):
                    self._push_expiry(session)
                    continue
                self.long_term_memory.promote_session(self._compat_session_view(session))
                expired_sessions.append(session_id)
                del self._sessions[session_id]
                self._locks.pop(session_id, None)
        return expired_sessions
