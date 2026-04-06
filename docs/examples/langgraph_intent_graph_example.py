from __future__ import annotations

"""
一个用 LangGraph 表达多意图执行图的最小示例。

示例能力：
- recognition 节点
- 两个可并行节点：query_account_balance / query_credit_bill
- 条件分支：余额足够 -> transfer_money，否则 -> notify_insufficient_balance
- human-in-the-loop：转账前 interrupt 等待用户确认
- persistence：用 InMemorySaver + thread_id 恢复同一条执行线程

说明：
- 这是演示如何用 LangGraph 承载“图运行时”
- 它没有实现真正的 LLM 意图识别和自动规划，只是把固定场景写成图

参考官方文档：
- Overview: https://docs.langchain.com/oss/python/langgraph/overview
- Graph API: https://docs.langchain.com/oss/python/langgraph/use-graph-api
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
"""

from typing import Annotated, Any, TypedDict
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


class RouterState(TypedDict, total=False):
    user_message: str
    transfer_amount: int
    recognized_intents: list[str]
    artifacts: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    events: Annotated[list[str], operator.add]
    balance_ok: bool


def recognize_intents(state: RouterState) -> dict[str, Any]:
    return {
        "recognized_intents": [
            "query_account_balance",
            "query_credit_bill",
            "transfer_money",
        ],
        "events": ["识别到余额查询、信用卡账单查询、转账三个相关能力"],
    }


def query_account_balance(state: RouterState) -> dict[str, Any]:
    balance = 3500
    return {
        "artifacts": {
            "query_account_balance": {
                "balance": balance,
                "currency": "CNY",
            }
        },
        "events": [f"工资卡余额查询完成，余额 {balance} 元"],
    }


def query_credit_bill(state: RouterState) -> dict[str, Any]:
    bill_amount = 888
    return {
        "artifacts": {
            "query_credit_bill": {
                "bill_amount": bill_amount,
                "currency": "CNY",
            }
        },
        "events": [f"信用卡账单查询完成，应还金额 {bill_amount} 元"],
    }


def decide_after_parallel(state: RouterState) -> dict[str, Any]:
    balance = state["artifacts"]["query_account_balance"]["balance"]
    amount = state["transfer_amount"]
    return {
        "balance_ok": balance >= amount,
        "events": [
            f"并行分支完成，开始判断余额是否足够执行 {amount} 元转账"
        ],
    }


def route_after_parallel(state: RouterState) -> str:
    if state["balance_ok"]:
        return "transfer_money"
    return "notify_insufficient_balance"


def transfer_money(state: RouterState) -> dict[str, Any]:
    amount = state["transfer_amount"]
    approval_payload = {
        "kind": "confirm_transfer",
        "message": f"检测到余额足够，是否确认向张三转账 {amount} 元？",
        "amount": amount,
        "recipient_name": "张三",
    }
    approved = interrupt(approval_payload)

    if not approved:
        return {
            "artifacts": {
                "transfer_money": {
                    "status": "cancelled",
                    "amount": amount,
                    "recipient_name": "张三",
                }
            },
            "events": ["用户拒绝转账，流程结束"],
        }

    return {
        "artifacts": {
            "transfer_money": {
                "status": "success",
                "amount": amount,
                "recipient_name": "张三",
            }
        },
        "events": [f"转账执行完成，已向张三转账 {amount} 元"],
    }


def notify_insufficient_balance(state: RouterState) -> dict[str, Any]:
    amount = state["transfer_amount"]
    balance = state["artifacts"]["query_account_balance"]["balance"]
    return {
        "artifacts": {
            "notify_insufficient_balance": {
                "status": "sent",
                "required_amount": amount,
                "current_balance": balance,
            }
        },
        "events": [f"余额不足：当前余额 {balance} 元，所需金额 {amount} 元"],
    }


def finalize(state: RouterState) -> dict[str, Any]:
    transfer = state.get("artifacts", {}).get("transfer_money", {})
    bill = state.get("artifacts", {}).get("query_credit_bill", {})
    summary = {
        "transfer": transfer or None,
        "credit_bill": bill or None,
    }
    return {
        "artifacts": {"finalize": summary},
        "events": ["流程完成，已生成汇总结果"],
    }


def build_graph():
    if not LANGGRAPH_AVAILABLE:
        raise RuntimeError(
            "langgraph is not installed. Run `pip install -U langgraph` first."
        )

    builder = StateGraph(RouterState)

    builder.add_node("recognize_intents", recognize_intents)
    builder.add_node("query_account_balance", query_account_balance)
    builder.add_node("query_credit_bill", query_credit_bill)
    builder.add_node("decide_after_parallel", decide_after_parallel)
    builder.add_node("transfer_money", transfer_money)
    builder.add_node("notify_insufficient_balance", notify_insufficient_balance)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "recognize_intents")
    builder.add_edge("recognize_intents", "query_account_balance")
    builder.add_edge("recognize_intents", "query_credit_bill")
    builder.add_edge("query_account_balance", "decide_after_parallel")
    builder.add_edge("query_credit_bill", "decide_after_parallel")
    builder.add_conditional_edges("decide_after_parallel", route_after_parallel)
    builder.add_edge("transfer_money", "finalize")
    builder.add_edge("notify_insufficient_balance", "finalize")
    builder.add_edge("finalize", END)

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
    print(json.dumps({k: v for k, v in state.items() if k != "__interrupt__"}, ensure_ascii=False, indent=2))


def main() -> None:
    if not LANGGRAPH_AVAILABLE:
        print("当前环境没有安装 langgraph。")
        print("如需运行本示例，请先执行: pip install -U langgraph")
        return

    graph = build_graph()
    config = {"configurable": {"thread_id": "intent-demo-thread-1"}}

    first_result = graph.invoke(
        {
            "user_message": (
                "先查余额，如果工资卡余额够 2000，就给张三转 2000；"
                "如果不够就提醒我余额不足。顺便再查一下信用卡账单。"
            ),
            "transfer_amount": 2000,
        },
        config=config,
    )
    dump_state("首次执行", first_result)

    if "__interrupt__" in first_result:
        resumed = graph.invoke(Command(resume=True), config=config)
        dump_state("确认转账后恢复执行", resumed)


if __name__ == "__main__":
    main()
