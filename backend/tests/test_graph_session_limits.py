from __future__ import annotations

from router_service.core.shared.domain import Task
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphSessionState,
    GraphStatus,
)


def test_graph_session_state_enforces_business_limit_by_trimming_oldest_suspended_business() -> None:
    session = GraphSessionState(session_id="session_limits", cust_id="cust_limits")

    for index in range(6):
        graph = ExecutionGraphState(source_message=f"graph-{index}", status=GraphStatus.RUNNING)
        graph.nodes.append(
            GraphNodeState(
                intent_code=f"intent_{index}",
                title=f"intent {index}",
                confidence=0.9,
            )
        )
        session.attach_business(graph, router_only_mode=False, pending=False)
        if index < 5:
            session.suspend_focus_business(reason=f"suspend-{index}")

    removed = session.enforce_business_limit(5)

    assert len(session.business_objects) == 5
    assert len(removed) == 1
    assert removed[0] not in session.workflow.suspended_business_ids
    assert session.focus_business() is not None


def test_graph_session_state_enforces_task_limit_without_removing_live_bound_task() -> None:
    session = GraphSessionState(session_id="session_tasks", cust_id="cust_tasks")
    protected_task = Task(
        session_id=session.session_id,
        intent_code="intent_protected",
        agent_url="http://agent/protected",
        confidence=0.9,
    )
    protected_task.task_id = "task-protected"
    session.tasks.append(protected_task)

    graph = ExecutionGraphState(source_message="source", status=GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent_protected",
        title="protected",
        confidence=0.9,
    )
    node.task_id = protected_task.task_id
    graph.nodes.append(node)
    session.attach_business(graph, router_only_mode=False, pending=False)

    for index in range(5):
        task = Task(
            session_id=session.session_id,
            intent_code=f"intent_{index}",
            agent_url=f"http://agent/{index}",
            confidence=0.5,
        )
        task.task_id = f"task-{index}"
        session.tasks.append(task)

    removed = session.enforce_task_limit(5)

    assert len(session.tasks) == 5
    assert protected_task in session.tasks
    assert removed == ["task-0"]


def test_graph_session_state_confirms_pending_business_and_updates_focus_aliases() -> None:
    session = GraphSessionState(session_id="session_pending", cust_id="cust_pending")
    graph = ExecutionGraphState(source_message="pending", status=GraphStatus.WAITING_CONFIRMATION)
    session.pending_graph = graph

    business = session.confirm_pending_business()

    assert business is not None
    assert session.pending_graph is None
    assert session.current_graph is graph
    assert session.workflow.pending_business_id is None
    assert session.workflow.focus_business_id == business.business_id


def test_graph_session_state_release_business_clears_graph_aliases_and_bound_tasks() -> None:
    session = GraphSessionState(session_id="session_release", cust_id="cust_release")
    graph = ExecutionGraphState(source_message="focus", status=GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent_release",
        title="release",
        confidence=0.9,
    )
    node.task_id = "task-release"
    graph.nodes.append(node)
    session.attach_business(graph, router_only_mode=False, pending=False)
    task = Task(
        session_id=session.session_id,
        intent_code="intent_release",
        agent_url="http://agent/release",
        confidence=0.9,
    )
    task.task_id = node.task_id
    session.tasks.append(task)

    released = session.release_business(session.focus_business().business_id)

    assert released is not None
    assert session.current_graph is None
    assert session.focus_business() is None
    assert session.tasks == []
