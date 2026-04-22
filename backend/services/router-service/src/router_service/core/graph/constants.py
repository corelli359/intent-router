from __future__ import annotations

from router_service.core.shared.graph_domain import GraphNodeStatus


TERMINAL_NODE_STATUSES = {
    GraphNodeStatus.READY_FOR_DISPATCH,
    GraphNodeStatus.COMPLETED,
    GraphNodeStatus.FAILED,
    GraphNodeStatus.CANCELLED,
    GraphNodeStatus.SKIPPED,
}

ACTIVE_NODE_STATUSES = {
    GraphNodeStatus.RUNNING,
    GraphNodeStatus.WAITING_USER_INPUT,
    GraphNodeStatus.WAITING_CONFIRMATION,
    GraphNodeStatus.WAITING_ASSISTANT_COMPLETION,
}
