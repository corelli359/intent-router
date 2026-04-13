from __future__ import annotations

import asyncio
from typing import Any

from router_service.core.graph.action_flow import GraphActionFlow
from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.shared.domain import Task, TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphNodeStatus,
    GraphRouterSnapshot,
    GraphSessionState,
    GraphStatus,
)


class DummyAgentClient:
    def __init__(self) -> None:
        self.cancelled_tasks: list[str] = []

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        self.cancelled_tasks.append(task_id)

    async def close(self) -> None:
        return None


class DummyEventPublisher:
    def __init__(self) -> None:
        self.graph_cancelled: list[str] = []

    async def publish_graph_cancelled(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        self.graph_cancelled.append(graph.graph_id)


class ActionFlowHelper:
    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []
        self.publisher = DummyEventPublisher()

    def activate_graph(self, graph: ExecutionGraphState) -> None:
        self.actions.append(("activate_graph", graph.graph_id))

    async def drain_graph(self, session: GraphSessionState, source_message: str) -> None:
        self.actions.append(("drain_graph", source_message))

    async def publish_node_state(
        self,
        session: GraphSessionState,
        graph: ExecutionGraphState,
        node: GraphNodeState,
        task_status: TaskStatus,
        event: str,
        message: str,
    ) -> None:
        self.actions.append(("publish_node_state", node.node_id, event))

    async def refresh_graph_state(self, session: GraphSessionState, graph: ExecutionGraphState) -> None:
        self.actions.append(("refresh_graph_state", graph.graph_id))

    async def emit_graph_progress(self, session: GraphSessionState) -> None:
        self.actions.append(("emit_graph_progress", session.session_id))

    async def publish_graph_state(
        self,
        session: GraphSessionState,
        event: str,
        message: str,
        *,
        status: TaskStatus | None = None,
    ) -> None:
        self.actions.append(("publish_graph_state", event))


def snapshot_builder(session_store: GraphSessionStore):
    def snapshot(session_id: str) -> GraphRouterSnapshot:
        session = session_store.get(session_id)
        return GraphRouterSnapshot(
            session_id=session.session_id,
            cust_id=session.cust_id,
            messages=list(session.messages),
            candidate_intents=list(session.candidate_intents),
            current_graph=session.current_graph,
            pending_graph=session.pending_graph,
            active_node_id=session.active_node_id,
            expires_at=session.expires_at,
        )

    return snapshot


def waiting_node_selector(session: GraphSessionState) -> GraphNodeState | None:
    graph = session.current_graph
    if graph is None:
        return None
    for node in graph.nodes:
        if node.status in {GraphNodeStatus.WAITING_USER_INPUT, GraphNodeStatus.WAITING_CONFIRMATION}:
            return node
    return None


def get_task(session: GraphSessionState, task_id: str | None) -> Task | None:
    if task_id is None:
        return None
    for task in session.tasks:
        if task.task_id == task_id:
            return task
    return None


def build_action_flow() -> tuple[GraphActionFlow, GraphSessionStore, ActionFlowHelper, DummyAgentClient]:
    session_store = GraphSessionStore()
    helper = ActionFlowHelper()
    agent_client = DummyAgentClient()
    flow = GraphActionFlow(
        session_store=session_store,
        agent_client=agent_client,
        event_publisher=helper.publisher,
        snapshot_session=snapshot_builder(session_store),
        get_waiting_node=waiting_node_selector,
        get_task=get_task,
        activate_graph=helper.activate_graph,
        drain_graph=helper.drain_graph,
        publish_node_state=helper.publish_node_state,
        refresh_graph_state=helper.refresh_graph_state,
        emit_graph_progress=helper.emit_graph_progress,
        publish_graph_state=helper.publish_graph_state,
    )
    return flow, session_store, helper, agent_client


def make_graph(status: GraphStatus) -> ExecutionGraphState:
    return ExecutionGraphState(source_message="source", status=status)


def test_confirm_pending_graph_activates_and_drains() -> None:
    flow, store, helper, _ = build_action_flow()
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.WAITING_CONFIRMATION)
    session.pending_graph = graph

    asyncio.run(flow.handle_action(session_id=session.session_id, cust_id=session.cust_id, action_code="confirm_graph"))

    assert session.pending_graph is None
    assert session.current_graph is graph
    assert ("activate_graph", graph.graph_id) in helper.actions
    assert ("drain_graph", graph.source_message) in helper.actions


def test_cancel_pending_graph_emits_cancel_event() -> None:
    flow, store, helper, _ = build_action_flow()
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.WAITING_CONFIRMATION)
    session.pending_graph = graph

    asyncio.run(flow.handle_action(session_id=session.session_id, cust_id=session.cust_id, action_code="cancel_graph"))

    assert graph.graph_id in helper.publisher.graph_cancelled


def test_cancel_current_node_triggers_agent_cancel() -> None:
    flow, store, helper, agent_client = build_action_flow()
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent",
        title="node",
        confidence=0.5,
        status=GraphNodeStatus.WAITING_USER_INPUT,
    )
    node.task_id = "task-1"
    graph.nodes.append(node)
    session.current_graph = graph
    session.tasks.append(Task(session_id=session.session_id, intent_code="intent", agent_url="http://agent", confidence=0.5))
    session.tasks[0].task_id = node.task_id
    session.tasks[0].status = TaskStatus.WAITING_USER_INPUT

    asyncio.run(flow.handle_action(session_id=session.session_id, cust_id=session.cust_id, action_code="cancel_node"))

    assert node.status == GraphNodeStatus.CANCELLED
    assert node.task_id in agent_client.cancelled_tasks
    assert ("publish_node_state", node.node_id, "node.cancelled") in helper.actions


def test_cancel_current_graph_marks_nodes_cancelled() -> None:
    flow, store, helper, agent_client = build_action_flow()
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.RUNNING)
    node1 = GraphNodeState(
        intent_code="intent_a",
        title="node A",
        confidence=0.4,
        status=GraphNodeStatus.RUNNING,
    )
    node1.task_id = "task-a"
    node2 = GraphNodeState(
        intent_code="intent_b",
        title="node B",
        confidence=0.6,
        status=GraphNodeStatus.READY,
    )
    node2.task_id = "task-b"
    graph.nodes.extend([node1, node2])
    session.current_graph = graph
    session.tasks.append(
        Task(
            session_id=session.session_id,
            intent_code="intent_a",
            agent_url="http://agent",
            confidence=0.5,
            status=TaskStatus.WAITING_USER_INPUT,
        )
    )
    session.tasks.append(
        Task(
            session_id=session.session_id,
            intent_code="intent_b",
            agent_url="http://agent",
            confidence=0.5,
            status=TaskStatus.WAITING_USER_INPUT,
        )
    )
    session.tasks[0].task_id = node1.task_id
    session.tasks[1].task_id = node2.task_id

    asyncio.run(flow.cancel_current_graph(session, reason="cleanup"))

    assert node1.status == GraphNodeStatus.CANCELLED
    assert node2.status == GraphNodeStatus.CANCELLED
    assert ("publish_graph_state", "graph.cancelled") in helper.actions
    assert node1.task_id in agent_client.cancelled_tasks
