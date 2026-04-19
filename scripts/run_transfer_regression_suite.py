#!/usr/bin/env python3
"""Run curated transfer regression scenarios against a live router HTTP endpoint."""

from __future__ import annotations

import argparse
import json
from time import perf_counter
import traceback
import urllib.error
import urllib.request
from typing import Any


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


class RouterClient:
    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def create_session(self) -> str:
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions",
            payload={},
            timeout_seconds=self.timeout_seconds,
        )
        return str(payload["session_id"])

    def post_message(self, *, session_id: str, content: str) -> dict[str, Any]:
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{session_id}/messages",
            payload={"content": content},
            timeout_seconds=self.timeout_seconds,
        )
        return payload["snapshot"]

    def post_action(self, *, session_id: str, action_code: str, task_id: str, confirm_token: str) -> dict[str, Any]:
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{session_id}/actions",
            payload={
                "task_id": task_id,
                "source": "router",
                "action_code": action_code,
                "confirm_token": confirm_token,
            },
            timeout_seconds=self.timeout_seconds,
        )
        return payload["snapshot"]


def _active_graph(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    current_graph = snapshot.get("current_graph")
    if isinstance(current_graph, dict):
        return current_graph
    pending_graph = snapshot.get("pending_graph")
    if isinstance(pending_graph, dict):
        return pending_graph
    return None


def _graph_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    graph = _active_graph(snapshot) or {}
    nodes = graph.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _last_message(snapshot: dict[str, Any]) -> str:
    messages = snapshot.get("messages") or []
    if not messages:
        return ""
    last = messages[-1]
    return str(last.get("content", "")) if isinstance(last, dict) else ""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_slots(node: dict[str, Any], expected: dict[str, str]) -> None:
    slot_memory = node.get("slot_memory") or {}
    _expect(isinstance(slot_memory, dict), f"slot_memory is not a dict: {slot_memory!r}")
    for slot_key, expected_value in expected.items():
        actual = str(slot_memory.get(slot_key))
        _expect(actual == expected_value, f"slot {slot_key} expected {expected_value!r}, got {actual!r}")


def _scenario_result(
    *,
    category: str,
    case_id: str,
    started_at: float,
    passed: bool,
    details: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "case_id": case_id,
        "passed": passed,
        "elapsed_ms": round((perf_counter() - started_at) * 1000, 2),
        "details": details,
        "error": error,
    }


def run_single_turn_basic(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "single_turn"
    case_id = "single_turn_basic_transfer"
    try:
        session_id = client.create_session()
        snapshot = client.post_message(session_id=session_id, content="给小红转200")
        graph = _active_graph(snapshot)
        _expect(graph is not None, "graph is missing")
        _expect(graph["status"] == "completed", f"expected completed, got {graph['status']!r}")
        nodes = _graph_nodes(snapshot)
        _expect(len(nodes) == 1, f"expected 1 node, got {len(nodes)}")
        _expect(nodes[0]["intent_code"] == "transfer_money", f"unexpected intent {nodes[0]['intent_code']!r}")
        _expect_slots(nodes[0], {"payee_name": "小红", "amount": "200"})
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "graph_status": graph["status"],
                "assistant_message": _last_message(snapshot),
                "slot_memory": nodes[0]["slot_memory"],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def run_single_turn_named_transfer(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "single_turn"
    case_id = "single_turn_named_transfer"
    try:
        session_id = client.create_session()
        snapshot = client.post_message(session_id=session_id, content="给王芳转300")
        graph = _active_graph(snapshot)
        _expect(graph is not None, "graph is missing")
        _expect(graph["status"] == "completed", f"expected completed, got {graph['status']!r}")
        nodes = _graph_nodes(snapshot)
        _expect(len(nodes) == 1, f"expected 1 node, got {len(nodes)}")
        _expect_slots(nodes[0], {"payee_name": "王芳", "amount": "300"})
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "graph_status": graph["status"],
                "assistant_message": _last_message(snapshot),
                "slot_memory": nodes[0]["slot_memory"],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def run_multi_turn_fill_all_missing(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "multi_turn"
    case_id = "multi_turn_fill_all_missing"
    try:
        session_id = client.create_session()
        first = client.post_message(session_id=session_id, content="帮我转账")
        first_graph = _active_graph(first)
        _expect(first_graph is not None, "first graph is missing")
        _expect(first_graph["status"] == "waiting_user_input", f"expected waiting_user_input, got {first_graph['status']!r}")
        _expect("金额" in _last_message(first), f"first prompt mismatch: {_last_message(first)!r}")
        second = client.post_message(session_id=session_id, content="给王芳转300")
        second_graph = _active_graph(second)
        _expect(second_graph is not None, "second graph is missing")
        _expect(second_graph["status"] == "completed", f"expected completed, got {second_graph['status']!r}")
        nodes = _graph_nodes(second)
        _expect(len(nodes) == 1, f"expected 1 node, got {len(nodes)}")
        _expect_slots(nodes[0], {"payee_name": "王芳", "amount": "300"})
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "first_prompt": _last_message(first),
                "assistant_message": _last_message(second),
                "slot_memory": nodes[0]["slot_memory"],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def run_multi_turn_fill_name_after_amount(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "multi_turn"
    case_id = "multi_turn_fill_name_after_amount"
    try:
        session_id = client.create_session()
        first = client.post_message(session_id=session_id, content="帮我转188")
        first_graph = _active_graph(first)
        _expect(first_graph is not None, "first graph is missing")
        _expect(first_graph["status"] == "waiting_user_input", f"expected waiting_user_input, got {first_graph['status']!r}")
        nodes = _graph_nodes(first)
        _expect_slots(nodes[0], {"amount": "188"})
        second = client.post_message(session_id=session_id, content="收款人李雷")
        second_graph = _active_graph(second)
        _expect(second_graph is not None, "second graph is missing")
        _expect(second_graph["status"] == "completed", f"expected completed, got {second_graph['status']!r}")
        nodes = _graph_nodes(second)
        _expect_slots(nodes[0], {"amount": "188", "payee_name": "李雷"})
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "first_prompt": _last_message(first),
                "assistant_message": _last_message(second),
                "slot_memory": nodes[0]["slot_memory"],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def run_multi_intent_single_turn(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "multi_intent_single_turn"
    case_id = "multi_intent_single_turn_two_transfers"
    try:
        session_id = client.create_session()
        first = client.post_message(session_id=session_id, content="先给妈妈转500，再给弟弟转600")
        graph = first.get("pending_graph")
        _expect(isinstance(graph, dict), "pending_graph is missing")
        _expect(graph["status"] == "waiting_confirmation", f"expected waiting_confirmation, got {graph['status']!r}")
        nodes = _graph_nodes(first)
        _expect(len(nodes) == 2, f"expected 2 nodes, got {len(nodes)}")
        _expect([node["intent_code"] for node in nodes] == ["transfer_money", "transfer_money"], "intent list mismatch")
        _expect_slots(nodes[0], {"payee_name": "妈妈", "amount": "500"})
        _expect_slots(nodes[1], {"payee_name": "弟弟", "amount": "600"})
        confirmed = client.post_action(
            session_id=session_id,
            action_code="confirm_graph",
            task_id=str(graph["graph_id"]),
            confirm_token=str(graph["confirm_token"]),
        )
        confirmed_graph = _active_graph(confirmed)
        _expect(confirmed_graph is not None, "confirmed graph is missing")
        _expect(confirmed_graph["status"] == "completed", f"expected completed, got {confirmed_graph['status']!r}")
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "pending_graph_status": graph["status"],
                "assistant_message": _last_message(confirmed),
                "final_node_statuses": [node["status"] for node in _graph_nodes(confirmed)],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def run_multi_intent_multi_turn(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "multi_intent_multi_turn"
    case_id = "multi_intent_multi_turn_two_transfers"
    try:
        session_id = client.create_session()
        first = client.post_message(session_id=session_id, content="先给妈妈转账，再给弟弟转账")
        graph = first.get("pending_graph")
        _expect(isinstance(graph, dict), "pending_graph is missing")
        nodes = _graph_nodes(first)
        _expect(len(nodes) == 2, f"expected 2 nodes, got {len(nodes)}")
        _expect_slots(nodes[0], {"payee_name": "妈妈"})
        _expect_slots(nodes[1], {"payee_name": "弟弟"})
        confirmed = client.post_action(
            session_id=session_id,
            action_code="confirm_graph",
            task_id=str(graph["graph_id"]),
            confirm_token=str(graph["confirm_token"]),
        )
        confirmed_graph = _active_graph(confirmed)
        _expect(confirmed_graph is not None, "confirmed graph is missing")
        _expect(confirmed_graph["status"] == "waiting_user_input", f"expected waiting_user_input, got {confirmed_graph['status']!r}")
        second = client.post_message(session_id=session_id, content="500")
        second_graph = _active_graph(second)
        _expect(second_graph is not None, "second graph is missing")
        second_nodes = _graph_nodes(second)
        _expect(second_nodes[0]["status"] == "completed", f"expected first node completed, got {second_nodes[0]['status']!r}")
        _expect_slots(second_nodes[0], {"payee_name": "妈妈", "amount": "500"})
        third = client.post_message(session_id=session_id, content="600")
        third_graph = _active_graph(third)
        _expect(third_graph is not None, "third graph is missing")
        _expect(third_graph["status"] == "completed", f"expected completed, got {third_graph['status']!r}")
        third_nodes = _graph_nodes(third)
        _expect_slots(third_nodes[1], {"payee_name": "弟弟", "amount": "600"})
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "first_prompt": _last_message(confirmed),
                "second_turn_message": _last_message(second),
                "third_turn_message": _last_message(third),
                "final_node_statuses": [node["status"] for node in third_nodes],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={},
            error=f"{type(exc).__name__}: {exc}",
        )


def run_intent_interleaving_resume(client: RouterClient) -> dict[str, Any]:
    started = perf_counter()
    category = "interleaving"
    case_id = "interleaving_suspend_resume_transfer"
    try:
        session_id = client.create_session()
        first = client.post_message(session_id=session_id, content="帮我给妈妈转账")
        first_graph = _active_graph(first)
        _expect(first_graph is not None, "first graph is missing")
        _expect(first_graph["status"] == "waiting_user_input", f"expected waiting_user_input, got {first_graph['status']!r}")
        first_nodes = _graph_nodes(first)
        _expect_slots(first_nodes[0], {"payee_name": "妈妈"})

        second = client.post_message(session_id=session_id, content="算了，先给弟弟转200")
        second_graph = _active_graph(second)
        _expect(second_graph is not None, "second graph is missing")
        second_nodes = _graph_nodes(second)
        _expect_slots(second_nodes[0], {"payee_name": "弟弟", "amount": "200"})

        third = client.post_message(session_id=session_id, content="继续刚才给妈妈那个，300")
        third_graph = _active_graph(third)
        _expect(third_graph is not None, "third graph is missing")
        third_nodes = _graph_nodes(third)
        _expect_slots(third_nodes[0], {"payee_name": "妈妈", "amount": "300"})
        _expect(third_graph["status"] == "completed", f"expected completed, got {third_graph['status']!r}")
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=True,
            details={
                "session_id": session_id,
                "assistant_messages": [
                    _last_message(first),
                    _last_message(second),
                    _last_message(third),
                ],
            },
        )
    except Exception as exc:
        return _scenario_result(
            category=category,
            case_id=case_id,
            started_at=started,
            passed=False,
            details={
                "traceback": traceback.format_exc(),
            },
            error=f"{type(exc).__name__}: {exc}",
        )


def build_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, int]] = {}
    for item in results:
        bucket = grouped.setdefault(item["category"], {"total": 0, "passed": 0, "failed": 0})
        bucket["total"] += 1
        if item["passed"]:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
    return {
        "total": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "failed": sum(1 for item in results if not item["passed"]),
        "categories": grouped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run curated transfer regression scenarios.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8012", help="Router base URL.")
    parser.add_argument("--timeout-seconds", type=float, default=240.0, help="Per-request timeout.")
    args = parser.parse_args()

    client = RouterClient(base_url=args.base_url, timeout_seconds=args.timeout_seconds)
    results = [
        run_single_turn_basic(client),
        run_single_turn_named_transfer(client),
        run_multi_turn_fill_all_missing(client),
        run_multi_turn_fill_name_after_amount(client),
        run_multi_intent_single_turn(client),
        run_multi_intent_multi_turn(client),
        run_intent_interleaving_resume(client),
    ]
    payload = {
        "base_url": args.base_url,
        "timeout_seconds": args.timeout_seconds,
        "summary": build_summary(results),
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
