from __future__ import annotations

from router_service.core.domain import TaskStatus
from router_service.core.v2_domain import (
    ExecutionGraphState,
    GraphCondition,
    GraphNodeSkipReason,
    GraphNodeState,
    GraphNodeStatus,
    GraphStatus,
)
from router_service.core.v2_graph_semantics import resolve_output_value


TERMINAL_NODE_STATUSES = {
    GraphNodeStatus.COMPLETED,
    GraphNodeStatus.FAILED,
    GraphNodeStatus.CANCELLED,
    GraphNodeStatus.SKIPPED,
}

ACTIVE_NODE_STATUSES = {
    GraphNodeStatus.RUNNING,
    GraphNodeStatus.WAITING_USER_INPUT,
    GraphNodeStatus.WAITING_CONFIRMATION,
}


class GraphRuntimeEngine:
    def activate_graph(self, graph: ExecutionGraphState) -> None:
        graph.actions = []
        self.refresh_node_states(graph)
        if graph.status == GraphStatus.WAITING_CONFIRMATION:
            graph.touch(GraphStatus.RUNNING)
        else:
            graph.touch(self.graph_status(graph))

    def refresh_node_states(self, graph: ExecutionGraphState) -> None:
        for node in graph.nodes:
            if node.status in TERMINAL_NODE_STATUSES | ACTIVE_NODE_STATUSES:
                continue
            incoming_edges = graph.incoming_edges(node.node_id)
            if not incoming_edges:
                node.touch(GraphNodeStatus.READY)
                continue

            should_skip = False
            all_ready = True
            blocking_reason = "等待上游节点完成"
            skip_reason_code: str | None = None
            for edge in incoming_edges:
                source = graph.node_by_id(edge.source_node_id)
                if source.status == GraphNodeStatus.FAILED:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    skip_reason_code = GraphNodeSkipReason.UPSTREAM_FAILED.value
                    break
                if source.status == GraphNodeStatus.CANCELLED:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    skip_reason_code = GraphNodeSkipReason.UPSTREAM_CANCELLED.value
                    break
                if source.status == GraphNodeStatus.SKIPPED:
                    should_skip = True
                    blocking_reason = edge.label or "上游节点未满足依赖"
                    skip_reason_code = GraphNodeSkipReason.UPSTREAM_SKIPPED.value
                    break
                expected_statuses = (
                    edge.condition.expected_statuses
                    if edge.condition is not None and edge.condition.expected_statuses
                    else [GraphNodeStatus.COMPLETED.value]
                )
                if source.status.value in expected_statuses:
                    if edge.condition is not None and edge.condition.left_key is not None:
                        if self.condition_matches(source, edge.condition):
                            continue
                        should_skip = True
                        blocking_reason = edge.label or "条件依赖未满足"
                        skip_reason_code = GraphNodeSkipReason.CONDITION_NOT_MET.value
                        break
                    continue
                if source.status in TERMINAL_NODE_STATUSES:
                    should_skip = True
                    blocking_reason = edge.label or "条件依赖未满足"
                    skip_reason_code = GraphNodeSkipReason.CONDITION_NOT_MET.value
                    break
                all_ready = False

            if should_skip:
                node.touch(
                    GraphNodeStatus.SKIPPED,
                    blocking_reason=blocking_reason,
                    skip_reason_code=skip_reason_code,
                )
            elif all_ready:
                node.touch(GraphNodeStatus.READY)
            else:
                node.touch(GraphNodeStatus.BLOCKED, blocking_reason=blocking_reason)

    def condition_matches(self, source: GraphNodeState, condition: GraphCondition | None) -> bool:
        if condition is None or condition.left_key is None or condition.operator is None:
            return False
        current_value = resolve_output_value(source.output_payload, condition.left_key)
        if current_value is None:
            return False
        threshold = condition.right_value
        operator = condition.operator
        if isinstance(current_value, (int, float)) and isinstance(threshold, (int, float)):
            left = float(current_value)
            right = float(threshold)
        else:
            left = current_value
            right = threshold
        try:
            if operator == ">":
                return left > right
            if operator == ">=":
                return left >= right
            if operator == "<":
                return left < right
            if operator == "<=":
                return left <= right
            return left == right
        except TypeError:
            return False

    def graph_status(self, graph: ExecutionGraphState) -> GraphStatus:
        statuses = [node.status for node in graph.nodes]
        if not statuses:
            return GraphStatus.COMPLETED
        if any(status == GraphNodeStatus.WAITING_CONFIRMATION for status in statuses):
            return GraphStatus.WAITING_CONFIRMATION_NODE
        if any(status == GraphNodeStatus.WAITING_USER_INPUT for status in statuses):
            return GraphStatus.WAITING_USER_INPUT
        if any(status in {GraphNodeStatus.READY, GraphNodeStatus.BLOCKED, GraphNodeStatus.RUNNING} for status in statuses):
            return GraphStatus.RUNNING
        if all(status in {GraphNodeStatus.CANCELLED, GraphNodeStatus.SKIPPED} for status in statuses):
            return GraphStatus.CANCELLED
        if all(status in {GraphNodeStatus.COMPLETED, GraphNodeStatus.SKIPPED} for status in statuses):
            return (
                GraphStatus.COMPLETED
                if self.all_skipped_nodes_are_condition_unmet(graph)
                else GraphStatus.PARTIALLY_COMPLETED
            )
        if any(status == GraphNodeStatus.FAILED for status in statuses):
            completed = any(status == GraphNodeStatus.COMPLETED for status in statuses)
            return GraphStatus.PARTIALLY_COMPLETED if completed else GraphStatus.FAILED
        if any(status == GraphNodeStatus.CANCELLED for status in statuses):
            completed = any(status == GraphNodeStatus.COMPLETED for status in statuses)
            return GraphStatus.PARTIALLY_COMPLETED if completed else GraphStatus.CANCELLED
        return GraphStatus.RUNNING

    def next_ready_node(self, graph: ExecutionGraphState) -> GraphNodeState | None:
        ready_nodes = [node for node in graph.nodes if node.status == GraphNodeStatus.READY]
        if not ready_nodes:
            return None
        ready_nodes.sort(key=lambda node: (node.position, node.created_at))
        return ready_nodes[0]

    def waiting_node(self, graph: ExecutionGraphState | None) -> GraphNodeState | None:
        if graph is None:
            return None
        waiting_nodes = [
            node
            for node in graph.nodes
            if node.status in {GraphNodeStatus.WAITING_USER_INPUT, GraphNodeStatus.WAITING_CONFIRMATION}
        ]
        if not waiting_nodes:
            return None
        waiting_nodes.sort(key=lambda node: node.updated_at, reverse=True)
        return waiting_nodes[0]

    def condition_skipped_nodes(self, graph: ExecutionGraphState) -> list[GraphNodeState]:
        return [
            node
            for node in graph.nodes
            if node.status == GraphNodeStatus.SKIPPED
            and node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value
        ]

    def all_skipped_nodes_are_condition_unmet(self, graph: ExecutionGraphState) -> bool:
        skipped_nodes = [node for node in graph.nodes if node.status == GraphNodeStatus.SKIPPED]
        return all(
            node.skip_reason_code == GraphNodeSkipReason.CONDITION_NOT_MET.value
            for node in skipped_nodes
        )

    def node_status_for_task_status(self, status: TaskStatus) -> GraphNodeStatus:
        mapping = {
            TaskStatus.CREATED: GraphNodeStatus.DRAFT,
            TaskStatus.QUEUED: GraphNodeStatus.READY,
            TaskStatus.DISPATCHING: GraphNodeStatus.RUNNING,
            TaskStatus.RUNNING: GraphNodeStatus.RUNNING,
            TaskStatus.WAITING_USER_INPUT: GraphNodeStatus.WAITING_USER_INPUT,
            TaskStatus.WAITING_CONFIRMATION: GraphNodeStatus.WAITING_CONFIRMATION,
            TaskStatus.RESUMING: GraphNodeStatus.RUNNING,
            TaskStatus.COMPLETED: GraphNodeStatus.COMPLETED,
            TaskStatus.FAILED: GraphNodeStatus.FAILED,
            TaskStatus.CANCELLED: GraphNodeStatus.CANCELLED,
        }
        return mapping[status]

    def task_status_for_graph(self, status: GraphStatus) -> TaskStatus:
        mapping = {
            GraphStatus.DRAFT: TaskStatus.CREATED,
            GraphStatus.WAITING_CONFIRMATION: TaskStatus.WAITING_CONFIRMATION,
            GraphStatus.RUNNING: TaskStatus.RUNNING,
            GraphStatus.WAITING_USER_INPUT: TaskStatus.WAITING_USER_INPUT,
            GraphStatus.WAITING_CONFIRMATION_NODE: TaskStatus.WAITING_CONFIRMATION,
            GraphStatus.PARTIALLY_COMPLETED: TaskStatus.COMPLETED,
            GraphStatus.COMPLETED: TaskStatus.COMPLETED,
            GraphStatus.FAILED: TaskStatus.FAILED,
            GraphStatus.CANCELLED: TaskStatus.CANCELLED,
        }
        return mapping[status]
