from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from router_service.core.graph.orchestrator import GraphRouterOrchestrator, GraphRouterOrchestratorConfig
from router_service.core.shared.domain import Task, TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphNodeStatus,
    GraphSessionState,
    GraphStatus,
)


def test_graph_orchestrator_drain_guard_fails_non_converging_graph() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(
            publish_event=lambda event: None,
            config=GraphRouterOrchestratorConfig(
                max_drain_iterations=2,
            ),
        )
        graph = ExecutionGraphState(source_message="测试 graph drain 护栏")
        node = GraphNodeState(
            intent_code="query_account_balance",
            title="查询余额",
            confidence=0.95,
            position=0,
            status=GraphNodeStatus.READY,
        )
        graph.nodes.append(node)
        session = GraphSessionState(
            session_id="session_guard",
            cust_id="cust_guard",
            current_graph=graph,
        )
        published_graph_events: list[tuple[str, str]] = []
        published_session_events: list[str] = []

        async def refresh_graph_state(session_arg: GraphSessionState, graph_arg: ExecutionGraphState) -> None:
            node.touch(GraphNodeStatus.READY)

        async def run_node(
            session_arg: GraphSessionState,
            graph_arg: ExecutionGraphState,
            node_arg: GraphNodeState,
            seed_input: str,
        ) -> None:
            node_arg.touch(GraphNodeStatus.COMPLETED)

        async def publish_graph_state(
            session_arg: GraphSessionState,
            event: str,
            message: str,
            *,
            status=None,
        ) -> None:
            published_graph_events.append((event, message))

        async def publish_session_state(session_arg: GraphSessionState, event: str) -> None:
            published_session_events.append(event)

        orchestrator._refresh_graph_state = AsyncMock(side_effect=refresh_graph_state)
        orchestrator._run_node = AsyncMock(side_effect=run_node)
        orchestrator._publish_graph_state = AsyncMock(side_effect=publish_graph_state)
        orchestrator._publish_session_state = AsyncMock(side_effect=publish_session_state)
        orchestrator._get_waiting_node = lambda session_arg: None
        orchestrator._next_ready_node = lambda graph_arg: node if node.status == GraphNodeStatus.READY else None

        await orchestrator._drain_graph(session, graph.source_message)

        assert graph.status == GraphStatus.FAILED
        assert session.active_node_id is None
        assert any("超过最大迭代次数" in message.content for message in session.messages)
        assert published_graph_events == [("graph.failed", session.messages[-1].content)]
        assert published_session_events == ["session.idle"]

    asyncio.run(run())


def test_graph_orchestrator_skips_full_refresh_for_non_terminal_agent_chunk() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        session = GraphSessionState(session_id="session_chunk", cust_id="cust_chunk")
        graph = ExecutionGraphState(source_message="测试 chunk")
        node = GraphNodeState(
            intent_code="transfer_money",
            title="转账",
            confidence=0.95,
            position=0,
            status=GraphNodeStatus.RUNNING,
        )
        task = Task(
            session_id=session.session_id,
            intent_code=node.intent_code,
            agent_url="https://agent.example.com/run",
            intent_name=node.title,
            intent_description="desc",
            confidence=node.confidence,
        )
        chunk = SimpleNamespace(
            status=TaskStatus.RUNNING,
            content="处理中",
            payload={},
            ishandover=False,
        )

        orchestrator.event_publisher.publish_node_runtime_event = AsyncMock()
        orchestrator._refresh_graph_state = AsyncMock()
        orchestrator._emit_graph_progress = AsyncMock()

        await orchestrator._handle_agent_chunk(session, graph, node, task, chunk)

        orchestrator.event_publisher.publish_node_runtime_event.assert_awaited_once()
        orchestrator._refresh_graph_state.assert_not_awaited()
        orchestrator._emit_graph_progress.assert_not_awaited()

    asyncio.run(run())


def test_graph_orchestrator_refreshes_graph_for_terminal_agent_chunk() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        session = GraphSessionState(session_id="session_chunk_terminal", cust_id="cust_chunk")
        graph = ExecutionGraphState(source_message="测试 chunk")
        node = GraphNodeState(
            intent_code="transfer_money",
            title="转账",
            confidence=0.95,
            position=0,
            status=GraphNodeStatus.RUNNING,
        )
        task = Task(
            session_id=session.session_id,
            intent_code=node.intent_code,
            agent_url="https://agent.example.com/run",
            intent_name=node.title,
            intent_description="desc",
            confidence=node.confidence,
        )
        chunk = SimpleNamespace(
            status=TaskStatus.COMPLETED,
            content="已完成",
            payload={},
            ishandover=True,
        )

        orchestrator.event_publisher.publish_node_runtime_event = AsyncMock()
        orchestrator._refresh_graph_state = AsyncMock()
        orchestrator._emit_graph_progress = AsyncMock()

        await orchestrator._handle_agent_chunk(session, graph, node, task, chunk)

        orchestrator._refresh_graph_state.assert_awaited_once()
        orchestrator._emit_graph_progress.assert_awaited_once()

    asyncio.run(run())


def test_graph_orchestrator_serialized_message_response_preserves_handover_graph_before_cleanup() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        graph = ExecutionGraphState(
            source_message="转 500 给王芳的卡",
            summary="router_only handover",
            status=GraphStatus.READY_FOR_DISPATCH,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.96,
                position=0,
                status=GraphNodeStatus.READY_FOR_DISPATCH,
                slot_memory={
                    "amount": "500",
                    "payee_name": "王芳",
                },
            )
        )

        async def handle_user_message(
            session_id: str,
            cust_id: str,
            content: str,
            *,
            router_only: bool,
            guided_selection=None,
            recommendation_context=None,
            proactive_recommendation=None,
            return_snapshot: bool,
        ) -> None:
            del content, guided_selection, recommendation_context, proactive_recommendation, return_snapshot
            session = orchestrator.session_store.get_or_create(session_id, cust_id)
            session.attach_business(graph, router_only_mode=router_only, pending=False)
            session.touch()

        orchestrator.message_flow.handle_user_message = AsyncMock(side_effect=handle_user_message)

        payload = await orchestrator.handle_user_message_serialized(
            session_id="session_router_only",
            cust_id="cust_router_only",
            content="转 500 给王芳的卡",
            router_only=True,
            serializer=lambda session: {
                "current_graph_status": session.current_graph.status.value if session.current_graph is not None else None,
                "node_status": session.current_graph.nodes[0].status.value if session.current_graph is not None else None,
                "slot_memory": dict(session.current_graph.nodes[0].slot_memory) if session.current_graph is not None else {},
                "shared_slot_memory": dict(session.shared_slot_memory),
            },
        )

        session = orchestrator.session_store.get("session_router_only")

        assert payload == {
            "current_graph_status": "ready_for_dispatch",
            "node_status": "ready_for_dispatch",
            "slot_memory": {
                "amount": "500",
                "payee_name": "王芳",
            },
            "shared_slot_memory": {},
        }
        assert session.current_graph is None
        assert session.pending_graph is None
        assert session.shared_slot_memory == {
            "amount": "500",
            "payee_name": "王芳",
        }
        assert session.business_memory_digests[-1].status == "ready_for_dispatch"

    asyncio.run(run())
