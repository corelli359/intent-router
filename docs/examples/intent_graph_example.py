from __future__ import annotations

"""
一个不依赖 LangGraph 的多意图执行图示例。

目标：
- 演示“多意图 -> ExecutionGraph”的建模方式
- 演示依赖、条件分支、受控并行的最小运行时
- 让示例结构尽量贴近本项目后续建议的数据模型

场景：
“先查余额，如果工资卡余额够 2000，就给张三转 2000；如果不够就提醒我余额不足。顺便再查一下信用卡账单。”
"""

from enum import StrEnum
from typing import Any, Literal
import json

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    INTENT_TASK = "intent_task"
    CONDITION = "condition"
    JOIN = "join"
    NOTIFY = "notify"


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ConditionExpression(BaseModel):
    left: str
    op: Literal[">", ">=", "==", "<", "<="]
    right: Any


class PlanNode(BaseModel):
    node_id: str
    node_type: NodeType
    title: str
    intent_code: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    condition: ConditionExpression | None = None
    run_if: ConditionExpression | None = None
    interactive: bool = True
    can_run_in_parallel: bool = False
    status: NodeStatus = NodeStatus.PENDING
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskArtifact(BaseModel):
    node_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutionGraph(BaseModel):
    source_message: str
    nodes: list[PlanNode] = Field(default_factory=list)
    artifacts: dict[str, TaskArtifact] = Field(default_factory=dict)

    def node_by_id(self, node_id: str) -> PlanNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(f"node not found: {node_id}")

    def artifact_payload(self, node_id: str) -> dict[str, Any]:
        artifact = self.artifacts.get(node_id)
        return artifact.payload if artifact is not None else {}


class GraphRuntime:
    def __init__(self, graph: ExecutionGraph) -> None:
        self.graph = graph

    def ready_nodes(self) -> list[PlanNode]:
        self._refresh_pending_nodes()

        has_active_foreground = any(
            node.interactive
            and node.status in {
                NodeStatus.RUNNING,
                NodeStatus.WAITING_USER_INPUT,
                NodeStatus.WAITING_CONFIRMATION,
            }
            for node in self.graph.nodes
        )

        ready: list[PlanNode] = []
        for node in self.graph.nodes:
            if node.status != NodeStatus.PENDING:
                continue
            if not self._dependencies_satisfied(node):
                continue
            if node.run_if is not None and not self._matches(node.run_if):
                continue
            if node.interactive and has_active_foreground:
                continue
            node.status = NodeStatus.READY
            ready.append(node)
        return ready

    def complete_node(self, node_id: str, payload: dict[str, Any]) -> None:
        node = self.graph.node_by_id(node_id)
        node.status = NodeStatus.COMPLETED
        self.graph.artifacts[node_id] = TaskArtifact(node_id=node_id, payload=payload)
        self._refresh_pending_nodes()

    def fail_node(self, node_id: str, reason: str) -> None:
        node = self.graph.node_by_id(node_id)
        node.status = NodeStatus.FAILED
        self.graph.artifacts[node_id] = TaskArtifact(
            node_id=node_id,
            payload={"reason": reason},
        )
        self._refresh_pending_nodes()

    def run_condition_node(self, node_id: str) -> None:
        node = self.graph.node_by_id(node_id)
        if node.condition is None:
            raise ValueError(f"condition node {node_id} has no condition")
        result = self._matches(node.condition)
        self.complete_node(node_id, {"result": result})

    def run_notify_node(self, node_id: str) -> None:
        node = self.graph.node_by_id(node_id)
        message = str(node.metadata.get("message", "notification"))
        self.complete_node(node_id, {"message": message})

    def snapshot(self) -> dict[str, Any]:
        return {
            "source_message": self.graph.source_message,
            "nodes": [node.model_dump(mode="json") for node in self.graph.nodes],
            "artifacts": {
                key: artifact.model_dump(mode="json")
                for key, artifact in self.graph.artifacts.items()
            },
        }

    def _refresh_pending_nodes(self) -> None:
        for node in self.graph.nodes:
            if node.status != NodeStatus.PENDING:
                continue
            if not self._dependencies_satisfied(node):
                continue
            if node.run_if is not None and not self._matches(node.run_if):
                node.status = NodeStatus.SKIPPED

    def _dependencies_satisfied(self, node: PlanNode) -> bool:
        terminal_ok = {NodeStatus.COMPLETED, NodeStatus.SKIPPED}
        return all(
            self.graph.node_by_id(dep_id).status in terminal_ok
            for dep_id in node.depends_on
        )

    def _matches(self, condition: ConditionExpression) -> bool:
        left_value = self._resolve_path(condition.left)
        right_value = condition.right
        if condition.op == ">":
            return left_value > right_value
        if condition.op == ">=":
            return left_value >= right_value
        if condition.op == "==":
            return left_value == right_value
        if condition.op == "<":
            return left_value < right_value
        if condition.op == "<=":
            return left_value <= right_value
        raise ValueError(f"unsupported operator: {condition.op}")

    def _resolve_path(self, path: str) -> Any:
        """
        只支持示例需要的路径形式：
        - artifacts.<node_id>.<field>
        """
        parts = [part for part in path.split(".") if part]
        if len(parts) < 3 or parts[0] != "artifacts":
            raise ValueError(f"unsupported path: {path}")
        node_id = parts[1]
        value: Any = self.graph.artifact_payload(node_id)
        for part in parts[2:]:
            if not isinstance(value, dict) or part not in value:
                raise KeyError(f"path not found: {path}")
            value = value[part]
        return value


def build_demo_graph(user_message: str) -> ExecutionGraph:
    return ExecutionGraph(
        source_message=user_message,
        nodes=[
            PlanNode(
                node_id="n1",
                node_type=NodeType.INTENT_TASK,
                title="查询工资卡余额",
                intent_code="query_account_balance",
                interactive=True,
                can_run_in_parallel=False,
                metadata={"slots": {"account_type": "salary_card"}},
            ),
            PlanNode(
                node_id="n2",
                node_type=NodeType.INTENT_TASK,
                title="查询信用卡账单",
                intent_code="query_credit_bill",
                interactive=False,
                can_run_in_parallel=True,
                metadata={"slots": {"account_type": "credit_card"}},
            ),
            PlanNode(
                node_id="n3",
                node_type=NodeType.CONDITION,
                title="判断余额是否足够",
                depends_on=["n1"],
                interactive=False,
                can_run_in_parallel=False,
                condition=ConditionExpression(
                    left="artifacts.n1.balance",
                    op=">=",
                    right=2000,
                ),
            ),
            PlanNode(
                node_id="n4",
                node_type=NodeType.INTENT_TASK,
                title="执行转账",
                intent_code="transfer_money",
                depends_on=["n3"],
                run_if=ConditionExpression(
                    left="artifacts.n3.result",
                    op="==",
                    right=True,
                ),
                interactive=True,
                can_run_in_parallel=False,
                metadata={
                    "slots": {
                        "recipient_name": "张三",
                        "amount": 2000,
                    }
                },
            ),
            PlanNode(
                node_id="n5",
                node_type=NodeType.NOTIFY,
                title="余额不足提醒",
                depends_on=["n3"],
                run_if=ConditionExpression(
                    left="artifacts.n3.result",
                    op="==",
                    right=False,
                ),
                interactive=False,
                can_run_in_parallel=False,
                metadata={"message": "余额不足，已跳过转账"},
            ),
            PlanNode(
                node_id="n6",
                node_type=NodeType.JOIN,
                title="汇总结果",
                depends_on=["n2", "n4", "n5"],
                interactive=False,
                can_run_in_parallel=False,
            ),
        ],
    )


def print_step(title: str, runtime: GraphRuntime) -> None:
    ready = runtime.ready_nodes()
    print(f"\n=== {title} ===")
    print("ready nodes:", [node.node_id for node in ready])
    print(
        json.dumps(
            runtime.snapshot(),
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    message = (
        "先查余额，如果工资卡余额够 2000，就给张三转 2000；"
        "如果不够就提醒我余额不足。顺便再查一下信用卡账单。"
    )
    runtime = GraphRuntime(build_demo_graph(message))

    print_step("初始化", runtime)

    runtime.complete_node("n2", {"bill_amount": 888.0, "currency": "CNY"})
    print_step("完成 n2 查询信用卡账单", runtime)

    runtime.complete_node("n1", {"balance": 3500.0, "currency": "CNY"})
    print_step("完成 n1 查询工资卡余额", runtime)

    runtime.run_condition_node("n3")
    print_step("完成 n3 条件判断", runtime)

    runtime.complete_node(
        "n4",
        {
            "transfer_status": "success",
            "recipient_name": "张三",
            "amount": 2000,
        },
    )
    print_step("完成 n4 转账", runtime)

    runtime.complete_node(
        "n6",
        {"summary": "余额查询完成，账单查询完成，转账完成"},
    )
    print_step("完成 n6 汇总", runtime)


if __name__ == "__main__":
    main()
