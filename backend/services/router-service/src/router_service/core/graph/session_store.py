from __future__ import annotations

from typing import Any
from uuid import uuid4

from router_service.core.shared.domain import utc_now
from router_service.core.shared.graph_domain import GraphSessionState
from router_service.core.support.memory_store import LongTermMemoryStore


class GraphSessionStore:
    """In-memory session store responsible for graph session lifecycle and expiry handling."""

    def __init__(self, long_term_memory: LongTermMemoryStore | None = None) -> None:
        """Initialize the session store and its long-term memory backend."""
        self._sessions: dict[str, GraphSessionState] = {}
        self.long_term_memory = long_term_memory or LongTermMemoryStore()

    def create(self, cust_id: str, session_id: str | None = None) -> GraphSessionState:
        """Create and store a fresh graph session."""
        resolved_session_id = session_id or f"session_graph_{uuid4().hex[:10]}"
        session = GraphSessionState(session_id=resolved_session_id, cust_id=cust_id)
        self._sessions[resolved_session_id] = session
        return session

    def get(self, session_id: str) -> GraphSessionState:
        """Return an existing graph session by id."""
        return self._sessions[session_id]

    def get_or_create(self, session_id: str | None, cust_id: str) -> GraphSessionState:
        """Resolve a session id, recreating expired or customer-mismatched sessions when needed."""
        if session_id is None:
            return self.create(cust_id=cust_id)
        if session_id not in self._sessions:
            self._sessions[session_id] = GraphSessionState(session_id=session_id, cust_id=cust_id)
        session = self._sessions[session_id]
        if session.cust_id != cust_id:
            session = GraphSessionState(session_id=session_id, cust_id=cust_id)
            self._sessions[session_id] = session
        if session.is_expired():
            self.long_term_memory.promote_session(self._compat_session_view(session))
            session = GraphSessionState(session_id=session.session_id, cust_id=session.cust_id)
            self._sessions[session_id] = session
        return session

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

    def purge_expired(self) -> list[str]:
        """Promote and remove any sessions whose TTL has elapsed."""
        now = utc_now()
        expired_sessions: list[str] = []
        for session_id, session in list(self._sessions.items()):
            if not session.is_expired(now=now):
                continue
            self.long_term_memory.promote_session(self._compat_session_view(session))
            expired_sessions.append(session_id)
            del self._sessions[session_id]
        return expired_sessions
