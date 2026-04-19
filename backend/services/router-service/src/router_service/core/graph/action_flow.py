from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

from router_service.core.support.agent_client import AgentClient
from router_service.core.shared.domain import Task, TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphNodeStatus,
    GraphRouterSnapshot,
    GraphSessionState,
    GraphStatus,
)
from router_service.core.graph.constants import TERMINAL_NODE_STATUSES
from router_service.core.graph.presentation import GraphEventPublisher
from router_service.core.graph.session_store import GraphSessionStore


logger = logging.getLogger(__name__)


class GraphActionFlow:
    """Handles explicit graph actions such as confirm/cancel and node interruption."""

    def __init__(
        self,
        *,
        session_store: GraphSessionStore,
        agent_client: AgentClient,
        event_publisher: GraphEventPublisher,
        snapshot_session: Callable[[str], GraphRouterSnapshot],
        get_waiting_node: Callable[[GraphSessionState], GraphNodeState | None],
        get_task: Callable[[GraphSessionState, str | None], Task | None],
        activate_graph: Callable[[ExecutionGraphState], None],
        drain_graph: Callable[[GraphSessionState, str], Awaitable[None]],
        publish_node_state: Callable[
            [GraphSessionState, ExecutionGraphState, GraphNodeState, TaskStatus, str, str],
            Awaitable[None],
        ],
        refresh_graph_state: Callable[[GraphSessionState, ExecutionGraphState], Awaitable[None]],
        emit_graph_progress: Callable[[GraphSessionState], Awaitable[None]],
        publish_graph_state: Callable[..., Awaitable[None]],
        memory_recall_limit: int = 20,
    ) -> None:
        """Initialize explicit graph action handlers and their collaborators."""
        self.session_store = session_store
        self.agent_client = agent_client
        self.event_publisher = event_publisher
        self.snapshot_session = snapshot_session
        self.get_waiting_node = get_waiting_node
        self.get_task = get_task
        self.activate_graph = activate_graph
        self.drain_graph = drain_graph
        self.publish_node_state = publish_node_state
        self.refresh_graph_state = refresh_graph_state
        self.emit_graph_progress = emit_graph_progress
        self.publish_graph_state = publish_graph_state
        self.memory_recall_limit = memory_recall_limit

    def _ensure_session_memory_warm(self, session: GraphSessionState) -> None:
        """Warm the session-scoped memory workset when the backing store supports it."""
        ensure_session_memory = getattr(self.session_store, "ensure_session_memory", None)
        if callable(ensure_session_memory):
            ensure_session_memory(session)
            return
        memory_runtime = getattr(self.session_store, "memory_runtime", None)
        if memory_runtime is None:
            return
        ensure_runtime_memory = getattr(memory_runtime, "ensure_session_memory", None)
        if callable(ensure_runtime_memory):
            ensure_runtime_memory(
                session_id=session.session_id,
                cust_id=session.cust_id,
                recall_limit=self.memory_recall_limit,
            )

    async def handle_action(
        self,
        *,
        session_id: str,
        cust_id: str,
        action_code: str,
        source: str | None = None,
        task_id: str | None = None,
        confirm_token: str | None = None,
        payload: dict[str, Any] | None = None,
        return_snapshot: bool = True,
    ) -> GraphRouterSnapshot | None:
        """Entry point for action APIs and graph-originated control actions."""
        session = self.session_store.get_or_create(session_id, cust_id)
        self._ensure_session_memory_warm(session)
        if source not in {None, "router", "graph"}:
            raise ValueError(f"Unsupported action source: {source}")

        if action_code in {"confirm_graph", "confirm_plan"}:
            await self.confirm_pending_graph(session, graph_id=task_id, confirm_token=confirm_token)
            return self.snapshot_session(session.session_id) if return_snapshot else None
        if action_code in {"cancel_graph", "cancel_plan"}:
            await self.cancel_pending_graph(session, graph_id=task_id, confirm_token=confirm_token)
            return self.snapshot_session(session.session_id) if return_snapshot else None
        if action_code == "cancel_node":
            await self.cancel_current_node(
                session,
                reason=(payload or {}).get("reason") or "用户取消当前节点",
            )
            return self.snapshot_session(session.session_id) if return_snapshot else None

        raise ValueError(f"Unsupported action_code: {action_code}")

    async def cancel_current_node(self, session: GraphSessionState, *, reason: str) -> None:
        """Cancel the currently waiting node and refresh graph progress."""
        graph = session.current_graph
        node = self.get_waiting_node(session)
        if graph is None or node is None:
            raise ValueError("No waiting node to cancel")
        task = self.get_task(session, node.task_id)
        if task is not None and task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}:
            # Only waiting tasks can be cancelled cooperatively at the agent side.
            # Running tasks are intentionally not interrupted here to avoid hiding
            # non-idempotent side effects behind an optimistic local cancel.
            try:
                await self.agent_client.cancel(session.session_id, task.task_id, task.agent_url)
            except Exception as exc:
                logger.warning("Failed to cancel node task %s: %s", task.task_id, exc)
            task.touch(TaskStatus.CANCELLED)
        node.touch(GraphNodeStatus.CANCELLED, blocking_reason=reason)
        await self.publish_node_state(session, graph, node, TaskStatus.CANCELLED, "node.cancelled", reason)
        await self.refresh_graph_state(session, graph)
        await self.emit_graph_progress(session)

    async def cancel_current_graph(self, session: GraphSessionState, *, reason: str) -> None:
        """Cancel all non-terminal nodes in the current graph."""
        graph = session.current_graph
        if graph is None:
            return
        cancel_failures: list[dict[str, str]] = []
        for node in graph.nodes:
            if node.status in TERMINAL_NODE_STATUSES:
                continue
            task = self.get_task(session, node.task_id)
            if task is not None and task.status in {TaskStatus.WAITING_USER_INPUT, TaskStatus.WAITING_CONFIRMATION}:
                try:
                    await self.agent_client.cancel(session.session_id, task.task_id, task.agent_url)
                except Exception as exc:
                    failure_reason = str(exc)
                    cancel_failures.append({"task_id": task.task_id, "reason": failure_reason})
                    logger.warning("Failed to cancel graph task %s: %s", task.task_id, exc)
                task.touch(TaskStatus.CANCELLED)
            node.touch(GraphNodeStatus.CANCELLED, blocking_reason=reason)
        graph.touch(GraphStatus.CANCELLED)
        session.clear_active_node()
        payload_overrides: dict[str, Any] | None = None
        event_message = reason or "执行图已取消"
        if cancel_failures:
            failure_items = "；".join(f"{item['task_id']}({item['reason']})" for item in cancel_failures)
            failure_summary = f"部分节点取消失败：{failure_items}"
            event_message = f"{event_message}；{failure_summary}"
            payload_overrides = {
                    "cancel_failures": cancel_failures,
                    "cancel_failure_summary": failure_summary,
            }
        business = session.business_for_graph(graph)
        if business is not None:
            business.sync_from_graph()
            session.release_business(business.business_id)
        else:
            session.clear_focus_graph_alias()
        await self.publish_graph_state(
            session,
            "graph.cancelled",
            event_message,
            status=TaskStatus.CANCELLED,
            payload_overrides=payload_overrides,
        )

    async def confirm_pending_graph(
        self,
        session: GraphSessionState,
        *,
        graph_id: str | None,
        confirm_token: str | None,
    ) -> None:
        """Promote `pending_graph` to `current_graph` and start draining it."""
        graph = session.pending_graph
        if graph is None or graph.status != GraphStatus.WAITING_CONFIRMATION:
            raise ValueError("No pending graph to confirm")
        if graph_id not in {None, "session", graph.graph_id}:
            raise ValueError("Invalid graph id for confirmation")
        if confirm_token is not None and confirm_token != graph.confirm_token:
            raise ValueError("Invalid graph confirm token")

        session.confirm_pending_business()
        self.activate_graph(graph)
        await self.publish_graph_state(session, "graph.confirmed", "执行图已确认，开始执行")
        await self.drain_graph(session, graph.source_message)

    async def cancel_pending_graph(
        self,
        session: GraphSessionState,
        *,
        graph_id: str | None,
        confirm_token: str | None,
    ) -> None:
        """Discard the proposed graph without executing any node."""
        graph = session.pending_graph
        if graph is None or graph.status != GraphStatus.WAITING_CONFIRMATION:
            raise ValueError("No pending graph to cancel")
        if graph_id not in {None, "session", graph.graph_id}:
            raise ValueError("Invalid graph id for cancellation")
        if confirm_token is not None and confirm_token != graph.confirm_token:
            raise ValueError("Invalid graph confirm token")

        graph.touch(GraphStatus.CANCELLED)
        graph.actions = []
        business = session.pending_business()
        if business is not None:
            business.sync_from_graph()
            session.release_business(business.business_id)
        else:
            session.clear_pending_graph_alias()
        await self.event_publisher.publish_graph_cancelled(session, graph)
