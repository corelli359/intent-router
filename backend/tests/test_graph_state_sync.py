from __future__ import annotations

import asyncio

from router_service.core.graph.presentation import GraphSnapshotPresenter
from router_service.core.graph.runtime import GraphRuntimeEngine
from router_service.core.graph.state_sync import GraphStateSync
from router_service.core.shared.domain import TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphNodeSkipReason,
    GraphNodeStatus,
    GraphSessionState,
    GraphStatus,
)


class DummyEventPublisher:
    def __init__(self) -> None:
        self.graph_events: list[tuple[str, str, TaskStatus | None]] = []
        self.node_events: list[tuple[str, str]] = []

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
        self.graph_events.append((event, message, status))

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
        self.node_events.append((event, node.node_id))

    async def publish_session_state(self, session: GraphSessionState, *, event: str) -> None:
        return None

    async def publish_pending_graph(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        return None


class DummySlotResolutionService:
    def apply_history_prefill_policy(self, *args, **kwargs) -> None:
        return None


def build_state_sync() -> tuple[GraphStateSync, DummyEventPublisher]:
    runtime_engine = GraphRuntimeEngine()
    presenter = GraphSnapshotPresenter(runtime_engine=runtime_engine)
    publisher = DummyEventPublisher()
    sync = GraphStateSync(
        runtime_engine=runtime_engine,
        presenter=presenter,
        event_publisher=publisher,
        slot_resolution_service=DummySlotResolutionService(),
    )
    return sync, publisher


def test_refresh_graph_state_publishes_skipped_node() -> None:
    sync, publisher = build_state_sync()
    graph = ExecutionGraphState(source_message="test", status=GraphStatus.RUNNING)
    failed_node = GraphNodeState(
        intent_code="intent_a",
        title="failed",
        confidence=0.5,
        status=GraphNodeStatus.FAILED,
    )
    depends_node = GraphNodeState(
        intent_code="intent_b",
        title="blocked",
        confidence=0.6,
        status=GraphNodeStatus.DRAFT,
        depends_on=[failed_node.node_id],
    )
    graph.nodes.extend([failed_node, depends_node])
    graph.edges.append(
        GraphEdge(
            source_node_id=failed_node.node_id,
            target_node_id=depends_node.node_id,
            relation_type=GraphEdgeType.SEQUENTIAL,
        )
    )
    session = GraphSessionState(session_id="session-id", cust_id="cust")
    session.current_graph = graph

    asyncio.run(sync.refresh_graph_state(session, graph))

    assert depends_node.status == GraphNodeStatus.SKIPPED
    assert any(event[0] == "node.skipped" for event in publisher.node_events)


def test_emit_graph_progress_appends_graph_terminal_message() -> None:
    sync, publisher = build_state_sync()
    graph = ExecutionGraphState(source_message="progress", status=GraphStatus.RUNNING)
    completed_node = GraphNodeState(
        intent_code="single",
        title="single node",
        confidence=0.9,
        status=GraphNodeStatus.COMPLETED,
    )
    skipped_node = GraphNodeState(
        intent_code="conditional",
        title="conditional node",
        confidence=0.8,
        status=GraphNodeStatus.SKIPPED,
        blocking_reason="余额不足",
        skip_reason_code=GraphNodeSkipReason.CONDITION_NOT_MET.value,
    )
    graph.nodes.extend([completed_node, skipped_node])
    session = GraphSessionState(session_id="session-id", cust_id="cust")
    session.current_graph = graph

    asyncio.run(sync.emit_graph_progress(session))

    assert publisher.graph_events
    assert session.messages
