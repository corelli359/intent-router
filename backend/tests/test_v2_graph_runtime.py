from __future__ import annotations

import sys
from pathlib import Path


from router_service.core.shared.graph_domain import (  # noqa: E402
    ExecutionGraphState,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphNodeStatus,
    GraphStatus,
)
from router_service.core.graph.runtime import GraphRuntimeEngine  # noqa: E402


def test_graph_runtime_engine_marks_condition_skip_as_completed_graph() -> None:
    engine = GraphRuntimeEngine()
    graph = ExecutionGraphState(source_message="测试条件跳过")
    balance = GraphNodeState(
        intent_code="query_account_balance",
        title="查询余额",
        confidence=0.98,
        position=0,
        status=GraphNodeStatus.COMPLETED,
        output_payload={"balance": 8000},
    )
    transfer = GraphNodeState(
        intent_code="transfer_money",
        title="转账",
        confidence=0.95,
        position=1,
    )
    transfer.depends_on.append(balance.node_id)
    graph.nodes.extend([balance, transfer])
    graph.edges.append(
        GraphEdge(
            source_node_id=balance.node_id,
            target_node_id=transfer.node_id,
            relation_type=GraphEdgeType.CONDITIONAL,
            label="余额大于20000时转账",
            condition=GraphCondition(
                source_node_id=balance.node_id,
                left_key="balance",
                operator=">",
                right_value=20000,
            ),
        )
    )

    engine.refresh_node_states(graph)

    assert transfer.status == GraphNodeStatus.SKIPPED
    assert engine.graph_status(graph) == GraphStatus.COMPLETED


def test_graph_runtime_engine_prefers_lowest_position_ready_node() -> None:
    engine = GraphRuntimeEngine()
    graph = ExecutionGraphState(source_message="测试 ready 选择")
    graph.nodes.extend(
        [
            GraphNodeState(
                intent_code="transfer_money",
                title="后面的节点",
                confidence=0.9,
                position=2,
                status=GraphNodeStatus.READY,
            ),
            GraphNodeState(
                intent_code="query_account_balance",
                title="前面的节点",
                confidence=0.9,
                position=0,
                status=GraphNodeStatus.READY,
            ),
        ]
    )

    next_node = engine.next_ready_node(graph)

    assert next_node is not None
    assert next_node.title == "前面的节点"
