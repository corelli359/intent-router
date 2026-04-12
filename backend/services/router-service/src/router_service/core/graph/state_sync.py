from __future__ import annotations

from typing import Any

from router_service.core.shared.domain import ChatMessage, IntentDefinition, TaskStatus, utc_now
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphCondition,
    GraphNodeSkipReason,
    GraphNodeState,
    GraphNodeStatus,
    GraphSessionState,
    GraphStatus,
    ProactiveRecommendationItem,
    ProactiveRecommendationPayload,
    SlotBindingSource,
    SlotBindingState,
)
from router_service.core.graph.presentation import GraphEventPublisher, GraphSnapshotPresenter
from router_service.core.graph.runtime import GraphRuntimeEngine
from router_service.core.slots.resolution_service import SlotResolutionService


class GraphStateSync:
    """Synchronizes runtime state changes with snapshot/event presentation.

    The orchestrator mutates graph/session/task state, while this helper is
    responsible for recomputing derived states and publishing consistent router
    events back to API consumers.
    """

    def __init__(
        self,
        *,
        runtime_engine: GraphRuntimeEngine,
        presenter: GraphSnapshotPresenter,
        event_publisher: GraphEventPublisher,
        slot_resolution_service: SlotResolutionService,
    ) -> None:
        self.runtime_engine = runtime_engine
        self.presenter = presenter
        self.event_publisher = event_publisher
        self.slot_resolution_service = slot_resolution_service

    async def publish_pending_graph(self, session: GraphSessionState) -> None:
        graph = session.pending_graph
        if graph is None:
            return
        await self.event_publisher.publish_pending_graph(session, graph)

    async def publish_graph_waiting_hint(self, session: GraphSessionState) -> None:
        graph = session.pending_graph
        if graph is None:
            return
        await self.event_publisher.publish_graph_waiting_hint(session, graph)

    async def publish_no_match_hint(self, session: GraphSessionState) -> None:
        """Append a human-readable no-match reply and publish the idle session event."""
        message = "暂未识别到明确事项，请换一种说法或补充更多上下文。"
        session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
        session.touch()
        await self.event_publisher.publish_unrecognized(session, message=message)
        await self.publish_session_state(session, "session.idle")

    async def publish_graph_state(
        self,
        session: GraphSessionState,
        event: str,
        message: str,
        *,
        status: TaskStatus | None = None,
    ) -> None:
        """Publish graph-level snapshots for either pending or current graph."""
        graph = session.pending_graph if event in {"graph.proposed", "graph.waiting_confirmation"} else session.current_graph
        if graph is None:
            return
        await self.event_publisher.publish_graph_state(
            session,
            graph,
            event=event,
            message=message,
            status=status,
            pending=graph is session.pending_graph,
        )

    async def publish_node_state(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        task_status: TaskStatus,
        event: str,
        message: str,
    ) -> None:
        await self.event_publisher.publish_node_state(
            session,
            graph,
            node,
            task_status=task_status,
            event=event,
            message=message,
        )

    async def publish_session_state(self, session: GraphSessionState, event: str) -> None:
        await self.event_publisher.publish_session_state(session, event=event)

    async def emit_graph_progress(self, session: GraphSessionState) -> None:
        """Re-derive graph status and publish any user-visible terminal message."""
        graph = session.current_graph
        if graph is None:
            return
        previous_status = graph.status
        graph.touch(self.graph_status(graph))
        event_name = self.presenter.graph_event_name(graph.status)
        message = self.presenter.graph_message(graph)
        if self.presenter.should_append_graph_terminal_message(graph, previous_status):
            session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
            session.touch()
        await self.publish_graph_state(session, event_name, message)

    def refresh_node_states(self, graph: ExecutionGraphState) -> None:
        self.runtime_engine.refresh_node_states(graph)

    def condition_matches_from_condition(self, source: GraphNodeState, condition: GraphCondition | None) -> bool:
        return self.runtime_engine.condition_matches(source, condition)

    def graph_status(self, graph: ExecutionGraphState) -> GraphStatus:
        return self.runtime_engine.graph_status(graph)

    def next_ready_node(self, graph: ExecutionGraphState) -> GraphNodeState | None:
        return self.runtime_engine.next_ready_node(graph)

    def get_waiting_node(self, session: GraphSessionState) -> GraphNodeState | None:
        return self.runtime_engine.waiting_node(session.current_graph)

    async def refresh_graph_state(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        """Refresh runtime-derived node/graph state after any node/task transition."""
        previous_statuses = {node.node_id: node.status for node in graph.nodes}
        self.refresh_node_states(graph)
        graph_status = self.graph_status(graph)

        for node in graph.nodes:
            previous_status = previous_statuses.get(node.node_id)
            if previous_status == node.status or node.status != GraphNodeStatus.SKIPPED:
                continue
            # When a node becomes skipped because of a failed condition or upstream
            # terminal state, clients should still receive an explicit node event.
            message = self.presenter.skipped_node_message(node)
            await self.publish_node_state(
                session,
                graph,
                node,
                TaskStatus.COMPLETED,
                "node.skipped",
                message,
            )
            if node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value and graph_status not in {
                GraphStatus.COMPLETED,
                GraphStatus.PARTIALLY_COMPLETED,
                GraphStatus.FAILED,
                GraphStatus.CANCELLED,
            }:
                session.messages.append(ChatMessage(role="assistant", content=message, created_at=utc_now()))
                session.touch()

        waiting_node = self.get_waiting_node(session)
        # `active_node_id` is a session-level shortcut used by the API/UI to know
        # whether the current blocker is a graph confirmation or a node-level slot prompt.
        session.active_node_id = waiting_node.node_id if waiting_node is not None else None

    def apply_history_prefill_policy(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        *,
        source_message: str,
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
    ) -> None:
        """Delegate history-based slot reuse into the shared slot resolution layer."""
        self.slot_resolution_service.apply_history_prefill_policy(
            session,
            graph,
            source_message=source_message,
            intents_by_code=intents_by_code,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
        )

    def history_slot_values(
        self,
        session: GraphSessionState,
        *,
        long_term_memory: list[str],
    ) -> dict[str, Any]:
        return self.slot_resolution_service.history_slot_values(
            session,
            long_term_memory=long_term_memory,
        )

    def structured_slot_bindings(
        self,
        *,
        slot_memory: dict[str, Any],
        source: SlotBindingSource,
        source_text: str | None,
        confidence: float,
    ) -> list[SlotBindingState]:
        return self.slot_resolution_service.structured_slot_bindings(
            slot_memory=slot_memory,
            source=source,
            source_text=source_text,
            confidence=confidence,
        )

    def rebuild_node_slot_bindings(
        self,
        node: GraphNodeState,
        *,
        preferred_sources: dict[str, SlotBindingSource] | None = None,
        source_text: str | None = None,
    ) -> None:
        self.slot_resolution_service.rebuild_node_slot_bindings(
            node,
            preferred_sources=preferred_sources,
            source_text=source_text,
        )

    def apply_proactive_slot_defaults(
        self,
        graph: ExecutionGraphState,
        *,
        selected_items: list[ProactiveRecommendationItem],
        proactive_recommendation: ProactiveRecommendationPayload | None,
        intents_by_code: dict[str, IntentDefinition],
    ) -> None:
        """Delegate proactive recommendation defaults into node slot memory."""
        self.slot_resolution_service.apply_proactive_slot_defaults(
            graph,
            selected_items=selected_items,
            proactive_recommendation=proactive_recommendation,
            intents_by_code=intents_by_code,
        )

    def node_status_for_task_status(self, status: TaskStatus) -> GraphNodeStatus:
        return self.runtime_engine.node_status_for_task_status(status)

    def task_status_for_graph(self, status: GraphStatus) -> TaskStatus:
        return self.runtime_engine.task_status_for_graph(status)
