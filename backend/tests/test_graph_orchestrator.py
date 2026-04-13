from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from router_service.core.graph.orchestrator import GraphRouterOrchestrator, GraphRouterOrchestratorConfig
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
