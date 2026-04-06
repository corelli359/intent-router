from __future__ import annotations

"""
一个真正按“graph factory”思路写的 LangGraph 示例。

这版示例解决的问题是：
- graph 不是固定 workflow
- graph 由 planner 产出的 execution_graph 规格动态构建
- agent 数量、条件节点、依赖关系都可以变化

核心接口：
- build_langgraph_from_execution_graph(spec, registry)

这才是“用 LangGraph 承载动态规划结果”的正确方向。

说明：
- 这个示例仍然用了 mock planner 和 mock agent registry
- 重点是 graph factory 的结构，而不是 planner 智能本身
"""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Callable, Literal, TypedDict
import json
import operator


def _merge_dicts(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - example fallback path
    LANGGRAPH_AVAILABLE = False
    InMemorySaver = None
    StateGraph = None
    START = None
    END = None
    Command = None
    interrupt = None


class NodeType(StrEnum):
    INTENT_TASK = "intent_task"
    CONDITION = "condition"
    HUMAN_GATE = "human_gate"
    NOTIFY = "notify"
    JOIN = "join"


class ConditionOperator(StrEnum):
    GT = ">"
    GTE = ">="
    EQ = "=="
    LT = "<"
    LTE = "<="


class RelationType(StrEnum):
    PARALLEL = "parallel"
    DEPENDS_ON = "depends_on"
    CONDITION_ON = "condition_on"


@dataclass(slots=True)
class ConditionSpec:
    left: str
    op: ConditionOperator
    right: Any


@dataclass(slots=True)
class GraphNodeSpec:
    node_id: str
    node_type: NodeType
    title: str
    depends_on: list[str] = field(default_factory=list)
    intent_code: str | None = None
    run_if: ConditionSpec | None = None
    condition: ConditionSpec | None = None
    interactive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionGraphSpec:
    graph_id: str
    source_message: str
    nodes: list[GraphNodeSpec] = field(default_factory=list)


@dataclass(slots=True)
class IntentCandidate:
    intent_code: str
    confidence: float
    title: str


@dataclass(slots=True)
class IntentRelation:
    source_intent: str
    target_intent: str
    relation_type: RelationType
    condition_text: str | None = None


@dataclass(slots=True)
class RecognitionResult:
    primary: list[IntentCandidate] = field(default_factory=list)
    candidates: list[IntentCandidate] = field(default_factory=list)


class LangGraphState(TypedDict, total=False):
    graph_id: str
    source_message: str
    artifacts: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    node_statuses: Annotated[dict[str, str], _merge_dicts]
    events: Annotated[list[str], operator.add]


AgentHandler = Callable[[GraphNodeSpec, LangGraphState], dict[str, Any]]


def resolve_path(path: str, artifacts: dict[str, dict[str, Any]]) -> Any:
    """
    只支持示例中的路径格式：
    artifacts.<node_id>.<field>
    """
    parts = [part for part in path.split(".") if part]
    if len(parts) < 3 or parts[0] != "artifacts":
        raise ValueError(f"unsupported path: {path}")
    node_id = parts[1]
    current: Any = artifacts[node_id]
    for part in parts[2:]:
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"path not found: {path}")
        current = current[part]
    return current


def eval_condition(condition: ConditionSpec, artifacts: dict[str, dict[str, Any]]) -> bool:
    left = resolve_path(condition.left, artifacts)
    right = condition.right
    if condition.op == ConditionOperator.GT:
        return left > right
    if condition.op == ConditionOperator.GTE:
        return left >= right
    if condition.op == ConditionOperator.EQ:
        return left == right
    if condition.op == ConditionOperator.LT:
        return left < right
    if condition.op == ConditionOperator.LTE:
        return left <= right
    raise ValueError(f"unsupported operator: {condition.op}")


def make_node_runner(
    node: GraphNodeSpec,
    registry: dict[str, AgentHandler],
) -> Callable[[LangGraphState], dict[str, Any]]:
    def run(state: LangGraphState) -> dict[str, Any]:
        artifacts = state.get("artifacts", {})

        if node.run_if is not None and not eval_condition(node.run_if, artifacts):
            return {
                "node_statuses": {node.node_id: "skipped"},
                "artifacts": {
                    node.node_id: {
                        "status": "skipped",
                        "reason": "run_if_not_matched",
                    }
                },
                "events": [f"{node.title} 被跳过，因为 run_if 不成立"],
            }

        if node.node_type == NodeType.CONDITION:
            if node.condition is None:
                raise ValueError(f"condition node {node.node_id} has no condition")
            result = eval_condition(node.condition, artifacts)
            return {
                "node_statuses": {node.node_id: "completed"},
                "artifacts": {node.node_id: {"result": result}},
                "events": [f"{node.title} 完成，结果={result}"],
            }

        if node.node_type == NodeType.HUMAN_GATE:
            if not LANGGRAPH_AVAILABLE or interrupt is None:
                raise RuntimeError("langgraph interrupt is unavailable")
            answer = interrupt(
                {
                    "kind": "human_gate",
                    "node_id": node.node_id,
                    "title": node.title,
                    "question": node.metadata.get("question", "请提供输入"),
                    "context": node.metadata,
                }
            )
            return {
                "node_statuses": {node.node_id: "completed"},
                "artifacts": {node.node_id: {"answer": answer}},
                "events": [f"{node.title} 已收到人工输入"],
            }

        if node.node_type == NodeType.NOTIFY:
            message = str(node.metadata.get("message", "notification"))
            return {
                "node_statuses": {node.node_id: "completed"},
                "artifacts": {node.node_id: {"message": message}},
                "events": [f"{node.title}: {message}"],
            }

        if node.node_type == NodeType.JOIN:
            joined = {
                dep_id: artifacts.get(dep_id, {})
                for dep_id in node.depends_on
            }
            return {
                "node_statuses": {node.node_id: "completed"},
                "artifacts": {node.node_id: {"summary": joined}},
                "events": [f"{node.title} 已聚合 {len(node.depends_on)} 个上游结果"],
            }

        if node.node_type == NodeType.INTENT_TASK:
            if not node.intent_code:
                raise ValueError(f"intent task {node.node_id} missing intent_code")
            handler = registry.get(node.intent_code)
            if handler is None:
                return {
                    "node_statuses": {node.node_id: "failed"},
                    "artifacts": {
                        node.node_id: {
                            "error": f"unregistered intent_code: {node.intent_code}"
                        }
                    },
                    "events": [f"{node.title} 执行失败：未注册 intent_code"],
                }
            result = handler(node, state)
            return {
                "node_statuses": {node.node_id: "completed"},
                "artifacts": {node.node_id: result},
                "events": [f"{node.title} 执行完成"],
            }

        raise ValueError(f"unsupported node_type: {node.node_type}")

    return run


def build_langgraph_from_execution_graph(
    spec: ExecutionGraphSpec,
    registry: dict[str, AgentHandler],
):
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(
            "langgraph is not installed. Run `pip install -U langgraph` first."
        )

    builder = StateGraph(LangGraphState)

    downstreams: dict[str, set[str]] = {node.node_id: set() for node in spec.nodes}
    for node in spec.nodes:
        builder.add_node(node.node_id, make_node_runner(node, registry))
        for dep_id in node.depends_on:
            if dep_id not in downstreams:
                downstreams[dep_id] = set()
            downstreams[dep_id].add(node.node_id)

    root_nodes = [node.node_id for node in spec.nodes if not node.depends_on]
    for node_id in root_nodes:
        builder.add_edge(START, node_id)

    for node in spec.nodes:
        for dep_id in node.depends_on:
            builder.add_edge(dep_id, node.node_id)

    leaf_nodes = [node_id for node_id, children in downstreams.items() if not children]
    for node_id in leaf_nodes:
        builder.add_edge(node_id, END)

    return builder.compile(checkpointer=InMemorySaver())


def mock_registry() -> dict[str, AgentHandler]:
    def query_account_balance(node: GraphNodeSpec, state: LangGraphState) -> dict[str, Any]:
        return {"balance": 3500, "currency": "CNY"}

    def query_credit_bill(node: GraphNodeSpec, state: LangGraphState) -> dict[str, Any]:
        return {"bill_amount": 888, "currency": "CNY"}

    def transfer_money(node: GraphNodeSpec, state: LangGraphState) -> dict[str, Any]:
        if node.metadata.get("require_confirmation"):
            if not LANGGRAPH_AVAILABLE or interrupt is None:
                raise RuntimeError("langgraph interrupt is unavailable")
            approved = interrupt(
                {
                    "kind": "confirm_transfer",
                    "node_id": node.node_id,
                    "title": node.title,
                    "slots": node.metadata.get("slots", {}),
                }
            )
            if not approved:
                return {
                    "status": "cancelled",
                    "reason": "user_rejected",
                }
        return {
            "status": "success",
            **node.metadata.get("slots", {}),
        }

    return {
        "query_account_balance": query_account_balance,
        "query_credit_bill": query_credit_bill,
        "transfer_money": transfer_money,
    }


def mock_multi_intent_recognizer(user_message: str) -> RecognitionResult:
    primary: list[IntentCandidate] = []
    if "余额" in user_message:
        primary.append(
            IntentCandidate(
                intent_code="query_account_balance",
                confidence=0.96,
                title="查询工资卡余额",
            )
        )
    if "账单" in user_message:
        primary.append(
            IntentCandidate(
                intent_code="query_credit_bill",
                confidence=0.92,
                title="查询信用卡账单",
            )
        )
    if "转账" in user_message or "转 2000" in user_message:
        primary.append(
            IntentCandidate(
                intent_code="transfer_money",
                confidence=0.95,
                title="执行转账",
            )
        )
    return RecognitionResult(primary=primary, candidates=[])


def mock_intent_relation_planner(
    user_message: str,
    recognition: RecognitionResult,
) -> list[IntentRelation]:
    intent_codes = {item.intent_code for item in recognition.primary}
    relations: list[IntentRelation] = []

    if {
        "query_account_balance",
        "query_credit_bill",
    }.issubset(intent_codes):
        relations.append(
            IntentRelation(
                source_intent="query_account_balance",
                target_intent="query_credit_bill",
                relation_type=RelationType.PARALLEL,
            )
        )

    if {
        "query_account_balance",
        "transfer_money",
    }.issubset(intent_codes) and "如果" in user_message:
        relations.append(
            IntentRelation(
                source_intent="query_account_balance",
                target_intent="transfer_money",
                relation_type=RelationType.CONDITION_ON,
                condition_text="余额 >= 2000 时才执行转账，否则发送余额不足提醒",
            )
        )

    return relations


def build_execution_graph_spec_from_recognition(
    *,
    user_message: str,
    recognition: RecognitionResult,
    relations: list[IntentRelation],
) -> ExecutionGraphSpec:
    """
    graph spec 是 recognizer + relation planner 的下游产物。
    这一步才是把“有哪些意图”和“它们之间是什么关系”编译成可执行图。
    """
    intent_codes = {item.intent_code for item in recognition.primary}
    relation_pairs = {
        (relation.source_intent, relation.target_intent, relation.relation_type)
        for relation in relations
    }

    nodes: list[GraphNodeSpec] = []

    if "query_account_balance" in intent_codes:
        nodes.append(
            GraphNodeSpec(
                node_id="n1",
                node_type=NodeType.INTENT_TASK,
                title="查询工资卡余额",
                intent_code="query_account_balance",
            )
        )

    if "query_credit_bill" in intent_codes:
        nodes.append(
            GraphNodeSpec(
                node_id="n2",
                node_type=NodeType.INTENT_TASK,
                title="查询信用卡账单",
                intent_code="query_credit_bill",
            )
        )

    if (
        "query_account_balance",
        "transfer_money",
        RelationType.CONDITION_ON,
    ) in relation_pairs:
        nodes.extend(
            [
                GraphNodeSpec(
                    node_id="n3",
                    node_type=NodeType.CONDITION,
                    title="判断余额是否足够",
                    depends_on=["n1"],
                    condition=ConditionSpec(
                        left="artifacts.n1.balance",
                        op=ConditionOperator.GTE,
                        right=2000,
                    ),
                ),
                GraphNodeSpec(
                    node_id="n4",
                    node_type=NodeType.INTENT_TASK,
                    title="执行转账",
                    intent_code="transfer_money",
                    depends_on=["n3"],
                    run_if=ConditionSpec(
                        left="artifacts.n3.result",
                        op=ConditionOperator.EQ,
                        right=True,
                    ),
                    interactive=True,
                    metadata={
                        "require_confirmation": True,
                        "slots": {
                            "recipient_name": "张三",
                            "amount": 2000,
                        },
                    },
                ),
                GraphNodeSpec(
                    node_id="n5",
                    node_type=NodeType.NOTIFY,
                    title="余额不足提醒",
                    depends_on=["n3"],
                    run_if=ConditionSpec(
                        left="artifacts.n3.result",
                        op=ConditionOperator.EQ,
                        right=False,
                    ),
                    metadata={"message": "余额不足，已跳过转账"},
                ),
            ]
        )

    join_deps = [
        node.node_id
        for node in nodes
        if node.node_type in {NodeType.INTENT_TASK, NodeType.NOTIFY}
    ]
    nodes.append(
        GraphNodeSpec(
            node_id="n6",
            node_type=NodeType.JOIN,
            title="汇总结果",
            depends_on=join_deps,
        )
    )

    return ExecutionGraphSpec(
        graph_id="graph_demo_v1",
        source_message=user_message,
        nodes=nodes,
    )


def dump_result(title: str, value: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    interrupt_payload = value.get("__interrupt__")
    if interrupt_payload:
        try:
            interrupt_value = interrupt_payload[0].value
        except Exception:
            interrupt_value = interrupt_payload
        print("interrupt:")
        print(json.dumps(interrupt_value, ensure_ascii=False, indent=2))
    printable = {k: v for k, v in value.items() if k != "__interrupt__"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    if not LANGGRAPH_AVAILABLE:
        print("当前环境没有安装 langgraph。")
        print("如需运行本示例，请先执行: pip install -U langgraph")
        return

    user_message = (
        "先查余额，如果工资卡余额够 2000，就给张三转 2000；"
        "如果不够就提醒我余额不足。顺便再查一下信用卡账单。"
    )

    recognition = mock_multi_intent_recognizer(user_message)
    relations = mock_intent_relation_planner(user_message, recognition)
    spec = build_execution_graph_spec_from_recognition(
        user_message=user_message,
        recognition=recognition,
        relations=relations,
    )

    print("=== 多意图识别结果 ===")
    print(
        json.dumps(
            {
                "primary": [asdict(item) for item in recognition.primary],
                "candidates": [asdict(item) for item in recognition.candidates],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("\n=== 意图关系推断结果 ===")
    print(
        json.dumps(
            [asdict(relation) for relation in relations],
            ensure_ascii=False,
            indent=2,
        )
    )
    print("\n=== ExecutionGraphSpec ===")
    print(
        json.dumps(
            {
                "graph_id": spec.graph_id,
                "source_message": spec.source_message,
                "nodes": [asdict(node) for node in spec.nodes],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    graph = build_langgraph_from_execution_graph(spec, mock_registry())
    config = {"configurable": {"thread_id": "dynamic-factory-demo-1"}}

    initial_state: LangGraphState = {
        "graph_id": spec.graph_id,
        "source_message": spec.source_message,
        "artifacts": {},
        "node_statuses": {},
        "events": [],
    }
    first_result = graph.invoke(initial_state, config=config)
    dump_result("首次执行", first_result)

    if "__interrupt__" in first_result:
        resumed = graph.invoke(Command(resume=True), config=config)
        dump_result("用户确认后恢复执行", resumed)


if __name__ == "__main__":
    main()
