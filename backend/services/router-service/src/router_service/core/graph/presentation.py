from __future__ import annotations

from collections.abc import Callable
from typing import Any

from router_service.core.shared.domain import TaskEvent, TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeSkipReason,
    GraphNodeState,
    GraphSessionState,
    GraphStatus,
)
from router_service.core.graph.runtime import GraphRuntimeEngine


class GraphSnapshotPresenter:
    """Convert graph runtime objects into user-facing event payloads and messages."""

    def __init__(self, runtime_engine: GraphRuntimeEngine | None = None) -> None:
        """Initialize the presenter with a runtime engine for derived graph facts."""
        self.runtime_engine = runtime_engine or GraphRuntimeEngine()

    def graph_event_name(self, status: GraphStatus) -> str:
        """Map graph status to the canonical SSE event name."""
        if status == GraphStatus.COMPLETED:
            return "graph.completed"
        if status == GraphStatus.PARTIALLY_COMPLETED:
            return "graph.partially_completed"
        if status == GraphStatus.FAILED:
            return "graph.failed"
        if status == GraphStatus.CANCELLED:
            return "graph.cancelled"
        return "graph.updated"

    def graph_message(self, graph: ExecutionGraphState) -> str:
        """Build the user-facing graph summary message for the current graph status."""
        status = graph.status
        condition_skips = self.runtime_engine.condition_skipped_nodes(graph)
        if status == GraphStatus.COMPLETED:
            if condition_skips:
                summaries = "；".join(
                    f"节点「{node.title}」因条件未满足未执行"
                    + (f"（{node.blocking_reason}）" if node.blocking_reason else "")
                    for node in condition_skips
                )
                return f"执行图已完成：{summaries}"
            return "执行图已完成"
        if status == GraphStatus.PARTIALLY_COMPLETED:
            return "执行图部分完成，存在已完成节点之外的未执行或异常终止节点"
        if status == GraphStatus.FAILED:
            return "执行图执行失败"
        if status == GraphStatus.CANCELLED:
            return "执行图已取消"
        if status == GraphStatus.WAITING_USER_INPUT:
            return "执行图等待用户补充信息"
        if status == GraphStatus.WAITING_CONFIRMATION_NODE:
            return "执行图等待节点确认"
        return "执行图状态更新"

    def should_append_graph_terminal_message(
        self,
        graph: ExecutionGraphState,
        previous_status: GraphStatus,
    ) -> bool:
        """Return whether the graph transition should append an assistant message."""
        if graph.status not in {
            GraphStatus.COMPLETED,
            GraphStatus.PARTIALLY_COMPLETED,
            GraphStatus.FAILED,
            GraphStatus.CANCELLED,
        }:
            return False
        if previous_status == graph.status:
            return False
        return graph.status != GraphStatus.COMPLETED or bool(self.runtime_engine.condition_skipped_nodes(graph))

    def skipped_node_message(self, node: GraphNodeState) -> str:
        """Build the user-facing message for a skipped node."""
        if node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value:
            if node.blocking_reason:
                return f"节点「{node.title}」未执行：条件不满足（{node.blocking_reason}）"
            return f"节点「{node.title}」未执行：条件不满足"
        if node.blocking_reason:
            return f"节点「{node.title}」已跳过（{node.blocking_reason}）"
        return f"节点「{node.title}」已跳过"

    def graph_payload(
        self,
        graph: ExecutionGraphState,
        *,
        include_actions: bool = False,
        pending: bool = False,
    ) -> dict[str, Any]:
        """Serialize one graph into the API/SSE payload shape."""
        payload = {
            "graph_id": graph.graph_id,
            "source_message": graph.source_message,
            "summary": graph.summary,
            "version": graph.version,
            "status": graph.status.value,
            "confirm_token": graph.confirm_token if pending else None,
            "nodes": [self.node_payload(node) for node in graph.nodes],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
        }
        if include_actions:
            payload["actions"] = [action.model_dump(mode="json") for action in graph.actions]
        return payload

    def node_payload(self, node: GraphNodeState) -> dict[str, Any]:
        """Serialize one node into the API/SSE payload shape."""
        return {
            "node_id": node.node_id,
            "intent_code": node.intent_code,
            "title": node.title,
            "confidence": node.confidence,
            "position": node.position,
            "source_fragment": node.source_fragment,
            "status": node.status.value,
            "task_id": node.task_id,
            "depends_on": list(node.depends_on),
            "blocking_reason": node.blocking_reason,
            "skip_reason_code": node.skip_reason_code,
            "relation_reason": node.relation_reason,
            "slot_memory": dict(node.slot_memory),
            "slot_bindings": [binding.model_dump(mode="json") for binding in node.slot_bindings],
            "history_slot_keys": list(node.history_slot_keys),
            "output_payload": dict(node.output_payload),
            "updated_at": node.updated_at.isoformat(),
        }

    def graph_interaction(self, graph: ExecutionGraphState, *, pending: bool) -> dict[str, Any]:
        """Build the frontend-oriented interaction payload for one graph card."""
        return {
            "type": "graph_card",
            "card_type": "dynamic_graph",
            "title": "请确认执行图" if pending else "动态执行图",
            "summary": graph.summary,
            "version": graph.version,
            "graph_id": graph.graph_id,
            "confirm_token": graph.confirm_token if pending else None,
            "nodes": [self.node_payload(node) for node in graph.nodes],
            "edges": [edge.model_dump(mode="json") for edge in graph.edges],
            "actions": [action.model_dump(mode="json") for action in graph.actions] if pending else [],
        }

    def normalize_interaction_payload(self, payload: dict[str, Any], *, source: str) -> dict[str, Any]:
        """Ensure interaction payloads carry a source marker for the frontend/client."""
        interaction = payload.get("interaction")
        if not isinstance(interaction, dict):
            return payload
        normalized = dict(payload)
        interaction_payload = dict(interaction)
        interaction_payload.setdefault("source", source)
        normalized["interaction"] = interaction_payload
        return normalized


class GraphEventPublisher:
    """Publish graph, node, and session events in the normalized SSE event shape."""

    def __init__(
        self,
        publish_event: Callable[[TaskEvent], Any],
        presenter: GraphSnapshotPresenter | None = None,
        runtime_engine: GraphRuntimeEngine | None = None,
    ) -> None:
        """Initialize the event publisher and its presenter/runtime helpers."""
        self.publish_event = publish_event
        self.presenter = presenter or GraphSnapshotPresenter(runtime_engine=runtime_engine)
        self.runtime_engine = runtime_engine or self.presenter.runtime_engine

    async def publish(self, event: TaskEvent) -> None:
        """Publish one event, supporting both sync and async publisher callbacks."""
        result = self.publish_event(event)
        if result is not None and hasattr(result, "__await__"):
            await result

    async def publish_recognition_started(self, session: GraphSessionState) -> None:
        """Publish the start of a recognition phase."""
        await self.publish(
            TaskEvent(
                event="recognition.started",
                task_id="recognition",
                session_id=session.session_id,
                intent_code="recognition",
                status=TaskStatus.RUNNING,
                message="开始意图识别",
                payload={"cust_id": session.cust_id},
            )
        )

    async def publish_recognition_delta(self, session: GraphSessionState, *, delta: str) -> None:
        """Publish one streamed recognition delta token or fragment."""
        if not delta:
            return
        await self.publish(
            TaskEvent(
                event="recognition.delta",
                task_id="recognition",
                session_id=session.session_id,
                intent_code="recognition",
                status=TaskStatus.RUNNING,
                message=delta,
                payload={"cust_id": session.cust_id},
            )
        )

    async def publish_recognition_completed(self, session: GraphSessionState, *, recognition: Any) -> None:
        """Publish the completed recognition result."""
        primary_intents = [match.intent_code for match in recognition.primary]
        await self.publish(
            TaskEvent(
                event="recognition.completed",
                task_id="recognition",
                session_id=session.session_id,
                intent_code="recognition",
                status=TaskStatus.COMPLETED,
                message=(
                    f"意图识别完成: {', '.join(primary_intents)}"
                    if primary_intents
                    else "意图识别完成: 未命中主意图"
                ),
                payload={
                    "cust_id": session.cust_id,
                    "primary": [match.model_dump() for match in recognition.primary],
                    "candidates": [match.model_dump() for match in recognition.candidates],
                },
            )
        )

    async def publish_graph_builder_started(self, session: GraphSessionState) -> None:
        """Publish the start of the unified graph builder phase."""
        await self.publish(
            TaskEvent(
                event="graph_builder.started",
                task_id="graph_builder",
                session_id=session.session_id,
                intent_code="graph_builder",
                status=TaskStatus.RUNNING,
                message="开始统一识别与建图",
                payload={"cust_id": session.cust_id},
            )
        )

    async def publish_graph_builder_delta(self, session: GraphSessionState, *, delta: str) -> None:
        """Publish one streamed delta from the unified graph builder."""
        if not delta:
            return
        await self.publish(
            TaskEvent(
                event="graph_builder.delta",
                task_id="graph_builder",
                session_id=session.session_id,
                intent_code="graph_builder",
                status=TaskStatus.RUNNING,
                message=delta,
                payload={"cust_id": session.cust_id},
            )
        )

    async def publish_graph_builder_completed(self, session: GraphSessionState, *, result: Any) -> None:
        """Publish the completed unified graph builder result."""
        await self.publish(
            TaskEvent(
                event="graph_builder.completed",
                task_id="graph_builder",
                session_id=session.session_id,
                intent_code="graph_builder",
                status=TaskStatus.COMPLETED,
                message="统一识别与建图完成",
                payload={
                    "cust_id": session.cust_id,
                    "primary": [match.model_dump() for match in result.recognition.primary],
                    "candidates": [match.model_dump() for match in result.recognition.candidates],
                    "graph": self.presenter.graph_payload(
                        result.graph,
                        include_actions=result.graph.status == GraphStatus.WAITING_CONFIRMATION,
                        pending=result.graph.status == GraphStatus.WAITING_CONFIRMATION,
                    ),
                },
            )
        )

    async def publish_graph_state(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        *,
        event: str,
        message: str,
        status: TaskStatus | None = None,
        pending: bool = False,
    ) -> None:
        """Publish one graph-level state update."""
        resolved_status = status or self.runtime_engine.task_status_for_graph(graph.status)
        await self.publish(
            TaskEvent(
                event=event,
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=resolved_status,
                message=message,
                ishandover=resolved_status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
                payload=self.presenter.normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "graph": self.presenter.graph_payload(
                            graph,
                            include_actions=graph.status == GraphStatus.WAITING_CONFIRMATION,
                            pending=pending,
                        ),
                        "interaction": self.presenter.graph_interaction(graph, pending=pending),
                    },
                    source="router",
                ),
            )
        )

    async def publish_node_runtime_event(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        *,
        task_status: TaskStatus,
        event: str,
        message: str | None,
        ishandover: bool = False,
        payload: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> None:
        """Publish one node-level runtime event with graph and node payload context."""
        event_payload: dict[str, Any] = {
            "cust_id": session.cust_id,
            **(payload or {}),
            "graph": self.presenter.graph_payload(graph),
            "node": self.presenter.node_payload(node),
        }
        if source is not None:
            event_payload = self.presenter.normalize_interaction_payload(event_payload, source=source)
        await self.publish(
            TaskEvent(
                event=event,
                task_id=node.node_id,
                session_id=session.session_id,
                intent_code=node.intent_code,
                status=task_status,
                message=message,
                ishandover=ishandover,
                payload=event_payload,
            )
        )

    async def publish_node_state(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        *,
        task_status: TaskStatus,
        event: str,
        message: str,
    ) -> None:
        """Publish a simplified node state transition event."""
        await self.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=task_status,
            event=event,
            message=message,
        )

    async def publish_session_state(self, session: GraphSessionState, *, event: str) -> None:
        """Publish a session-level state update."""
        payload: dict[str, Any] = {
            "cust_id": session.cust_id,
            "active_node_id": session.active_node_id,
            "candidate_intents": [match.model_dump() for match in session.candidate_intents],
            "expires_at": session.expires_at.isoformat(),
        }
        if session.current_graph is not None:
            payload["graph"] = self.presenter.graph_payload(session.current_graph)
        if session.pending_graph is not None:
            payload["pending_graph"] = self.presenter.graph_payload(
                session.pending_graph,
                include_actions=True,
                pending=True,
            )
        await self.publish(
            TaskEvent(
                event=event,
                task_id="session",
                session_id=session.session_id,
                intent_code="session",
                status=TaskStatus.RUNNING if session.active_node_id else TaskStatus.COMPLETED,
                message="会话状态更新",
                payload=payload,
            )
        )

    async def publish_unrecognized(self, session: GraphSessionState, *, message: str) -> None:
        """Publish the router's no-match outcome."""
        await self.publish(
            TaskEvent(
                event="graph.unrecognized",
                task_id="graph",
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.COMPLETED,
                message=message,
                ishandover=True,
                payload={"cust_id": session.cust_id},
            )
        )

    async def publish_pending_graph(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        """Publish a newly proposed pending graph that requires confirmation."""
        await self.publish(
            TaskEvent(
                event="graph.proposed",
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.WAITING_CONFIRMATION,
                message="请确认执行图",
                ishandover=False,
                payload=self.presenter.normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "graph": self.presenter.graph_payload(graph, include_actions=True, pending=True),
                        "interaction": self.presenter.graph_interaction(graph, pending=True),
                    },
                    source="router",
                ),
            )
        )

    async def publish_graph_waiting_hint(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        """Publish a reminder that a pending graph still awaits confirmation."""
        await self.publish(
            TaskEvent(
                event="graph.waiting_confirmation",
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.WAITING_CONFIRMATION,
                message="当前有待确认的执行图，请先确认或取消",
                ishandover=False,
                payload=self.presenter.normalize_interaction_payload(
                    {
                        "cust_id": session.cust_id,
                        "graph": self.presenter.graph_payload(graph, include_actions=True, pending=True),
                        "interaction": self.presenter.graph_interaction(graph, pending=True),
                    },
                    source="router",
                ),
            )
        )

    async def publish_graph_cancelled(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        """Publish cancellation of a pending or current graph."""
        await self.publish(
            TaskEvent(
                event="graph.cancelled",
                task_id=graph.graph_id,
                session_id=session.session_id,
                intent_code="graph",
                status=TaskStatus.CANCELLED,
                message="已取消执行图",
                ishandover=True,
                payload=self.presenter.normalize_interaction_payload(
                    {"cust_id": session.cust_id, "graph": self.presenter.graph_payload(graph)},
                    source="router",
                ),
            )
        )
