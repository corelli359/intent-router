from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


from router_service.core.shared.domain import TaskEvent, TaskStatus  # noqa: E402
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState, GraphNodeStatus, GraphStatus  # noqa: E402
from router_service.core.graph.presentation import GraphEventPublisher, GraphSnapshotPresenter  # noqa: E402


def test_graph_snapshot_presenter_reports_condition_skip_summary() -> None:
    presenter = GraphSnapshotPresenter()
    graph = ExecutionGraphState(source_message="测试")
    balance = GraphNodeState(
        intent_code="query_account_balance",
        title="查询余额",
        confidence=0.98,
        position=0,
        status=GraphNodeStatus.COMPLETED,
    )
    transfer = GraphNodeState(
        intent_code="transfer_money",
        title="转账给妈妈",
        confidence=0.95,
        position=1,
        status=GraphNodeStatus.SKIPPED,
        skip_reason_code="condition_not_met",
        blocking_reason="余额大于20000时转账",
    )
    graph.nodes.extend([balance, transfer])
    graph.status = GraphStatus.COMPLETED

    message = presenter.graph_message(graph)

    assert "因条件未满足未执行" in message
    assert presenter.should_append_graph_terminal_message(graph, GraphStatus.RUNNING) is True


def test_graph_event_publisher_publishes_session_state_payload() -> None:
    events: list[TaskEvent] = []
    presenter = GraphSnapshotPresenter()
    publisher = GraphEventPublisher(events.append, presenter)

    async def run() -> None:
        from router_service.core.shared.graph_domain import GraphSessionState

        session = GraphSessionState(session_id="s1", cust_id="cust_demo")
        graph = ExecutionGraphState(source_message="测试图", status=GraphStatus.RUNNING)
        graph.nodes.append(
            GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.9,
                position=0,
            )
        )
        session.current_graph = graph
        session.active_node_id = graph.nodes[0].node_id
        await publisher.publish_session_state(session, event="session.waiting_user_input")

    asyncio.run(run())

    assert len(events) == 1
    assert events[0].event == "session.waiting_user_input"
    assert events[0].status == TaskStatus.RUNNING
    assert events[0].payload["graph"]["graph_id"]


def test_graph_event_publisher_skips_session_payload_when_scope_disabled() -> None:
    events: list[TaskEvent] = []
    publisher = GraphEventPublisher(events.append)

    async def run() -> None:
        from router_service.core.shared.graph_domain import GraphSessionState

        session = GraphSessionState(session_id="s1", cust_id="cust_demo")
        graph = ExecutionGraphState(source_message="测试图", status=GraphStatus.RUNNING)
        graph.nodes.append(
            GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.9,
                position=0,
            )
        )
        session.current_graph = graph
        with publisher.event_scope(False):
            await publisher.publish_session_state(session, event="session.updated")

    asyncio.run(run())

    assert events == []


def test_graph_event_publisher_publishes_recognition_and_node_runtime_events() -> None:
    events: list[TaskEvent] = []
    publisher = GraphEventPublisher(events.append)

    async def run() -> None:
        from router_service.core.shared.graph_domain import GraphSessionState

        session = GraphSessionState(session_id="s1", cust_id="cust_demo")
        graph = ExecutionGraphState(source_message="查余额后转账", status=GraphStatus.RUNNING)
        node = GraphNodeState(
            intent_code="query_account_balance",
            title="查询余额",
            confidence=0.96,
            position=0,
            status=GraphNodeStatus.WAITING_USER_INPUT,
        )
        graph.nodes.append(node)
        recognition = SimpleNamespace(
            primary=[SimpleNamespace(intent_code="query_account_balance", model_dump=lambda: {"intent_code": "query_account_balance"})],
            candidates=[SimpleNamespace(model_dump=lambda: {"intent_code": "transfer_money"})],
        )
        await publisher.publish_recognition_started(session)
        await publisher.publish_recognition_delta(session, delta="正在分析上下文")
        await publisher.publish_recognition_completed(session, recognition=recognition)
        await publisher.publish_node_runtime_event(
            session,
            graph,
            node,
            task_status=TaskStatus.WAITING_USER_INPUT,
            event="node.waiting_user_input",
            message="请提供卡号",
            payload={"interaction": {"type": "form"}},
            source="agent",
        )

    asyncio.run(run())

    assert [event.event for event in events] == [
        "recognition.started",
        "recognition.delta",
        "recognition.completed",
        "node.waiting_user_input",
    ]
    assert events[2].payload["primary"][0]["intent_code"] == "query_account_balance"
    assert events[3].payload["interaction"]["source"] == "agent"
    assert events[3].payload["node"]["intent_code"] == "query_account_balance"
