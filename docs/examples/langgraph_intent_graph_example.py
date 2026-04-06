from __future__ import annotations

"""
一个更贴近真实需求的 LangGraph 示例。

核心思想：
- LangGraph 外层只保留少数几个“运行时节点”
- 真正的业务执行图存放在 state["execution_graph"] 里
- 业务 agent、依赖关系、条件分支都不是写死在 LangGraph 边上
- `pick_ready_nodes()` 根据 execution_graph 动态选择 ready 节点
- `run_node()` 是通用执行器，根据 node_type / intent_code 动态执行

这才更接近“很多个 agent、很多条件、动态规划”的真实场景。

固定的是：
- recognize_or_update_goal
- plan_graph
- pick_ready_nodes
- run_node
- finish

动态的是：
- execution_graph 里到底有哪些节点
- 每个节点调用哪个 agent
- 节点之间怎样依赖
- 哪些条件成立后走哪条支路
- 哪个节点需要 human-in-the-loop

参考：
- Overview: https://docs.langchain.com/oss/python/langgraph/overview
- Graph API: https://docs.langchain.com/oss/python/langgraph/use-graph-api
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
"""

from typing import Annotated, Any, Literal, TypedDict, cast
import json
import operator


def _merge_dicts(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _append_unique(
    left: list[dict[str, Any]] | None,
    right: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    result = list(left or [])
    for item in right or []:
        result.append(item)
    return result


try:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, Send, interrupt

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - example fallback path
    LANGGRAPH_AVAILABLE = False
    InMemorySaver = None
    StateGraph = None
    START = None
    END = None
    Command = None
    Send = None
    interrupt = None


class ConditionSpec(TypedDict):
    left: str
    op: Literal[">", ">=", "==", "<", "<="]
    right: Any


class DynamicNode(TypedDict, total=False):
    node_id: str
    node_type: Literal["intent_task", "condition", "notify", "join"]
    title: str
    intent_code: str
    depends_on: list[str]
    run_if: ConditionSpec | None
    condition: ConditionSpec | None
    interactive: bool
    can_run_in_parallel: bool
    require_confirmation: bool
    status: Literal[
        "pending",
        "ready",
        "running",
        "waiting_user_input",
        "waiting_confirmation",
        "completed",
        "failed",
        "cancelled",
        "skipped",
    ]
    metadata: dict[str, Any]


class RouterState(TypedDict, total=False):
    user_message: str
    plan_version: int
    recognized_intents: list[str]
    execution_graph: Annotated[list[DynamicNode], _append_unique]
    artifacts: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    events: Annotated[list[str], operator.add]
    active_foreground_node_id: str | None
    final_summary: dict[str, Any]


class RunNodeState(TypedDict, total=False):
    node_id: str
    execution_graph: list[DynamicNode]
    artifacts: dict[str, dict[str, Any]]
    user_message: str


def node_by_id(nodes: list[DynamicNode], node_id: str) -> DynamicNode:
    for node in nodes:
        if node["node_id"] == node_id:
            return node
    raise KeyError(f"node not found: {node_id}")


def update_node_status(
    nodes: list[DynamicNode],
    node_id: str,
    status: str,
) -> list[DynamicNode]:
    updated: list[DynamicNode] = []
    for node in nodes:
        copied = dict(node)
        if copied["node_id"] == node_id:
            copied["status"] = status
        updated.append(copied)
    return updated


def mock_recognizer(user_message: str) -> list[str]:
    recognized: list[str] = []
    if "余额" in user_message:
        recognized.append("query_account_balance")
    if "账单" in user_message:
        recognized.append("query_credit_bill")
    if "转" in user_message or "转账" in user_message:
        recognized.append("transfer_money")
    return recognized


def mock_dynamic_planner(user_message: str, intents: list[str]) -> list[DynamicNode]:
    """
    模拟一个“动态 planner”输出。

    重点不是 planner 智能本身，而是：
    - LangGraph 外层并不知道这里会出现哪些 node_id
    - 也不知道有哪些 intent_code
    - 只是在 runtime 里消费 planner 产出的 execution_graph
    """
    nodes: list[DynamicNode] = []

    if "query_account_balance" in intents:
        nodes.append(
            {
                "node_id": "n1",
                "node_type": "intent_task",
                "title": "查询工资卡余额",
                "intent_code": "query_account_balance",
                "depends_on": [],
                "interactive": False,
                "can_run_in_parallel": True,
                "require_confirmation": False,
                "status": "pending",
                "metadata": {"slots": {"account_type": "salary_card"}},
            }
        )

    if "query_credit_bill" in intents:
        nodes.append(
            {
                "node_id": "n2",
                "node_type": "intent_task",
                "title": "查询信用卡账单",
                "intent_code": "query_credit_bill",
                "depends_on": [],
                "interactive": False,
                "can_run_in_parallel": True,
                "require_confirmation": False,
                "status": "pending",
                "metadata": {"slots": {"account_type": "credit_card"}},
            }
        )

    if "transfer_money" in intents and "如果" in user_message:
        nodes.append(
            {
                "node_id": "n3",
                "node_type": "condition",
                "title": "判断余额是否足够",
                "depends_on": ["n1"],
                "condition": {
                    "left": "artifacts.n1.balance",
                    "op": ">=",
                    "right": 2000,
                },
                "interactive": False,
                "can_run_in_parallel": False,
                "require_confirmation": False,
                "status": "pending",
                "metadata": {},
            }
        )
        nodes.append(
            {
                "node_id": "n4",
                "node_type": "intent_task",
                "title": "执行转账",
                "intent_code": "transfer_money",
                "depends_on": ["n3"],
                "run_if": {
                    "left": "artifacts.n3.result",
                    "op": "==",
                    "right": True,
                },
                "interactive": True,
                "can_run_in_parallel": False,
                "require_confirmation": True,
                "status": "pending",
                "metadata": {
                    "slots": {
                        "recipient_name": "张三",
                        "amount": 2000,
                    }
                },
            }
        )
        nodes.append(
            {
                "node_id": "n5",
                "node_type": "notify",
                "title": "余额不足提醒",
                "depends_on": ["n3"],
                "run_if": {
                    "left": "artifacts.n3.result",
                    "op": "==",
                    "right": False,
                },
                "interactive": False,
                "can_run_in_parallel": False,
                "require_confirmation": False,
                "status": "pending",
                "metadata": {"message": "余额不足，已跳过转账"},
            }
        )

    nodes.append(
        {
            "node_id": "n_final",
            "node_type": "join",
            "title": "汇总结果",
            "depends_on": [
                node["node_id"]
                for node in nodes
                if node["node_type"] in {"intent_task", "notify"}
            ],
            "interactive": False,
            "can_run_in_parallel": False,
            "require_confirmation": False,
            "status": "pending",
            "metadata": {},
        }
    )
    return nodes


def resolve_path(path: str, artifacts: dict[str, dict[str, Any]]) -> Any:
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
    left = resolve_path(condition["left"], artifacts)
    right = condition["right"]
    op = condition["op"]
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "==":
        return left == right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    raise ValueError(f"unsupported operator: {op}")


def graph_terminal(nodes: list[DynamicNode]) -> bool:
    terminal = {"completed", "failed", "cancelled", "skipped"}
    return all(node["status"] in terminal for node in nodes)


def refresh_graph(nodes: list[DynamicNode], artifacts: dict[str, dict[str, Any]]) -> list[DynamicNode]:
    terminal_ok = {"completed", "skipped"}
    refreshed: list[DynamicNode] = []
    for node in nodes:
        copied = dict(node)
        if copied["status"] != "pending":
            refreshed.append(copied)
            continue

        deps = copied.get("depends_on", [])
        deps_satisfied = all(
            node_by_id(nodes, dep_id)["status"] in terminal_ok
            for dep_id in deps
        )
        if not deps_satisfied:
            refreshed.append(copied)
            continue

        run_if = copied.get("run_if")
        if run_if is not None and not eval_condition(run_if, artifacts):
            copied["status"] = "skipped"
        refreshed.append(copied)
    return refreshed


def pick_ready_node_ids(
    nodes: list[DynamicNode],
    artifacts: dict[str, dict[str, Any]],
) -> list[str]:
    nodes = refresh_graph(nodes, artifacts)
    has_active_foreground = any(
        node.get("interactive", False)
        and node["status"] in {"running", "waiting_user_input", "waiting_confirmation"}
        for node in nodes
    )

    ready: list[str] = []
    terminal_ok = {"completed", "skipped"}
    for node in nodes:
        if node["status"] != "pending":
            continue
        deps = node.get("depends_on", [])
        deps_satisfied = all(
            node_by_id(nodes, dep_id)["status"] in terminal_ok
            for dep_id in deps
        )
        if not deps_satisfied:
            continue
        run_if = node.get("run_if")
        if run_if is not None and not eval_condition(run_if, artifacts):
            continue
        if node.get("interactive", False) and has_active_foreground:
            continue
        ready.append(node["node_id"])
    return ready


def recognize_or_update_goal(state: RouterState) -> dict[str, Any]:
    intents = mock_recognizer(state["user_message"])
    return {
        "recognized_intents": intents,
        "events": [f"识别完成，候选主意图: {', '.join(intents)}"],
    }


def plan_graph(state: RouterState) -> dict[str, Any]:
    current_version = int(state.get("plan_version", 0))
    nodes = mock_dynamic_planner(state["user_message"], state["recognized_intents"])
    return {
        "plan_version": current_version + 1,
        "execution_graph": nodes,
        "events": [f"planner 已生成 execution_graph，节点数 {len(nodes)}"],
    }


def pick_ready_nodes(state: RouterState):
    nodes = refresh_graph(state["execution_graph"], state.get("artifacts", {}))
    ready_ids = pick_ready_node_ids(nodes, state.get("artifacts", {}))

    if graph_terminal(nodes):
        return Command(
            update={
                "execution_graph": nodes,
                "events": ["execution_graph 已进入终态，准备结束"],
            },
            goto="finish",
        )

    if not ready_ids:
        return Command(
            update={
                "execution_graph": nodes,
                "events": ["当前没有新的 ready 节点，等待后续输入或已有节点恢复"],
            },
            goto="finish",
        )

    sends = [
        Send(
            "run_node",
            {
                "node_id": node_id,
                "execution_graph": nodes,
                "artifacts": state.get("artifacts", {}),
                "user_message": state["user_message"],
            },
        )
        for node_id in ready_ids
    ]
    return Command(
        update={
            "execution_graph": nodes,
            "events": [f"调度 ready 节点: {', '.join(ready_ids)}"],
        },
        goto=sends,
    )


def run_node(state: RunNodeState) -> dict[str, Any]:
    nodes = cast(list[DynamicNode], state["execution_graph"])
    artifacts = cast(dict[str, dict[str, Any]], state.get("artifacts", {}))
    node = node_by_id(nodes, state["node_id"])
    nodes = update_node_status(nodes, state["node_id"], "running")

    if node["node_type"] == "condition":
        condition = cast(ConditionSpec, node["condition"])
        result = eval_condition(condition, artifacts)
        nodes = update_node_status(nodes, node["node_id"], "completed")
        return {
            "execution_graph": nodes,
            "artifacts": {node["node_id"]: {"result": result}},
            "events": [f"{node['title']} 完成，结果为 {result}"],
        }

    if node["node_type"] == "notify":
        message = str(node.get("metadata", {}).get("message", "notification"))
        nodes = update_node_status(nodes, node["node_id"], "completed")
        return {
            "execution_graph": nodes,
            "artifacts": {node["node_id"]: {"message": message}},
            "events": [f"{node['title']} 已发送: {message}"],
        }

    if node["node_type"] == "join":
        summary = {
            key: value
            for key, value in artifacts.items()
            if key in node.get("depends_on", [])
        }
        nodes = update_node_status(nodes, node["node_id"], "completed")
        return {
            "execution_graph": nodes,
            "artifacts": {node["node_id"]: {"summary": summary}},
            "events": ["join 节点完成，已聚合上游结果"],
        }

    if node["node_type"] == "intent_task":
        intent_code = node["intent_code"]
        metadata = node.get("metadata", {})
        if intent_code == "query_account_balance":
            payload = {"balance": 3500, "currency": "CNY"}
        elif intent_code == "query_credit_bill":
            payload = {"bill_amount": 888, "currency": "CNY"}
        elif intent_code == "transfer_money":
            if node.get("require_confirmation"):
                approved = interrupt(
                    {
                        "kind": "confirm_transfer",
                        "node_id": node["node_id"],
                        "message": "检测到余额足够，是否确认执行转账？",
                        "slots": metadata.get("slots", {}),
                    }
                )
                if not approved:
                    nodes = update_node_status(nodes, node["node_id"], "cancelled")
                    return {
                        "execution_graph": nodes,
                        "artifacts": {
                            node["node_id"]: {
                                "status": "cancelled",
                                "reason": "user_rejected",
                            }
                        },
                        "events": [f"{node['title']} 被用户取消"],
                    }
            payload = {
                "status": "success",
                **metadata.get("slots", {}),
            }
        else:
            payload = {"status": "ok", "intent_code": intent_code}

        nodes = update_node_status(nodes, node["node_id"], "completed")
        return {
            "execution_graph": nodes,
            "artifacts": {node["node_id"]: payload},
            "events": [f"{node['title']} 执行完成"],
        }

    raise ValueError(f"unsupported node_type: {node['node_type']}")


def finish(state: RouterState) -> dict[str, Any]:
    final_summary = {
        "plan_version": state.get("plan_version"),
        "artifacts": state.get("artifacts", {}),
        "terminal": graph_terminal(state.get("execution_graph", [])),
    }
    return {
        "final_summary": final_summary,
        "events": ["外层 LangGraph runtime 已结束本轮运行"],
    }


def build_graph():
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(
            "langgraph is not installed. Run `pip install -U langgraph` first."
        )

    builder = StateGraph(RouterState)
    builder.add_node("recognize_or_update_goal", recognize_or_update_goal)
    builder.add_node("plan_graph", plan_graph)
    builder.add_node("pick_ready_nodes", pick_ready_nodes)
    builder.add_node("run_node", run_node)
    builder.add_node("finish", finish)

    builder.add_edge(START, "recognize_or_update_goal")
    builder.add_edge("recognize_or_update_goal", "plan_graph")
    builder.add_edge("plan_graph", "pick_ready_nodes")
    builder.add_edge("run_node", "pick_ready_nodes")
    builder.add_edge("finish", END)

    return builder.compile(checkpointer=InMemorySaver())


def dump_state(title: str, state: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    interrupt_payload = state.get("__interrupt__")
    if interrupt_payload:
        try:
            interrupt_value = interrupt_payload[0].value
        except Exception:
            interrupt_value = interrupt_payload
        print("interrupt:", json.dumps(interrupt_value, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {key: value for key, value in state.items() if key != "__interrupt__"},
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    if not LANGGRAPH_AVAILABLE:
        print("当前环境没有安装 langgraph。")
        print("如需运行本示例，请先执行: pip install -U langgraph")
        return

    graph = build_graph()
    config = {"configurable": {"thread_id": "dynamic-intent-router-demo-1"}}

    initial_state: RouterState = {
        "user_message": (
            "先查余额，如果工资卡余额够 2000，就给张三转 2000；"
            "如果不够就提醒我余额不足。顺便再查一下信用卡账单。"
        ),
        "artifacts": {},
        "events": [],
    }

    first_result = graph.invoke(initial_state, config=config)
    dump_state("首次执行", first_result)

    if "__interrupt__" in first_result:
        resumed = graph.invoke(Command(resume=True), config=config)
        dump_state("用户确认后恢复执行", resumed)


if __name__ == "__main__":
    main()
