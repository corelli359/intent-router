#!/usr/bin/env python3
"""Run scenario-driven multi-turn router-only dialog verification."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any
import urllib.error
import urllib.request


DEFAULT_BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://intent-router.kkrrc-359.top")
DEFAULT_HOST_HEADER = os.getenv("INTENT_ROUTER_HOST_HEADER")
DEFAULT_CUST_ID = os.getenv("INTENT_ROUTER_CUST_ID", "cust_demo")
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("INTENT_ROUTER_TIMEOUT_SECONDS", "180"))
DEFAULT_INTERACTIVE = os.getenv("INTENT_ROUTER_INTERACTIVE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}


@dataclass(frozen=True)
class TurnExpectation:
    user_input: str
    expected_stage: str
    expected_intent_code: str
    expected_slots: dict[str, str]
    expected_reply: str


@dataclass(frozen=True)
class DialogScenario:
    name: str
    description: str
    turns: tuple[TurnExpectation, ...]
    final_required_slots: dict[str, str]


SCENARIOS: tuple[DialogScenario, ...] = (
    DialogScenario(
        name="single_turn_all_slots",
        description="一轮给全姓名和金额，应直接 ready。",
        turns=(
            TurnExpectation(
                user_input="给小明转500元",
                expected_stage="ready",
                expected_intent_code="AG_TRANS",
                expected_slots={"payee_name": "小明", "amount": "500"},
                expected_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_required_slots={"payee_name": "小明", "amount": "500"},
    ),
    DialogScenario(
        name="two_turn_name_then_amount",
        description="第一轮给收款人，第二轮补金额。",
        turns=(
            TurnExpectation(
                user_input="给小明转账",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={"payee_name": "小明"},
                expected_reply="请提供金额",
            ),
            TurnExpectation(
                user_input="200",
                expected_stage="ready",
                expected_intent_code="AG_TRANS",
                expected_slots={"payee_name": "小明", "amount": "200"},
                expected_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_required_slots={"payee_name": "小明", "amount": "200"},
    ),
    DialogScenario(
        name="two_turn_amount_then_name",
        description="第一轮给金额，第二轮补收款人。",
        turns=(
            TurnExpectation(
                user_input="我要转账200元",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={"amount": "200"},
                expected_reply="请提供收款人姓名",
            ),
            TurnExpectation(
                user_input="给小明",
                expected_stage="ready",
                expected_intent_code="AG_TRANS",
                expected_slots={"amount": "200", "payee_name": "小明"},
                expected_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_required_slots={"payee_name": "小明", "amount": "200"},
    ),
    DialogScenario(
        name="three_turn_generic_name_then_amount",
        description="第一轮只表达转账意图，后两轮依次补收款人和金额。",
        turns=(
            TurnExpectation(
                user_input="我要转账",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={},
                expected_reply="请提供金额、收款人姓名",
            ),
            TurnExpectation(
                user_input="给小明",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={"payee_name": "小明"},
                expected_reply="请提供金额",
            ),
            TurnExpectation(
                user_input="200",
                expected_stage="ready",
                expected_intent_code="AG_TRANS",
                expected_slots={"payee_name": "小明", "amount": "200"},
                expected_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_required_slots={"payee_name": "小明", "amount": "200"},
    ),
    DialogScenario(
        name="three_turn_generic_amount_then_name",
        description="第一轮只表达转账意图，后两轮依次补金额和收款人。",
        turns=(
            TurnExpectation(
                user_input="我要转账",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={},
                expected_reply="请提供金额、收款人姓名",
            ),
            TurnExpectation(
                user_input="200",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={"amount": "200"},
                expected_reply="请提供收款人姓名",
            ),
            TurnExpectation(
                user_input="给小明",
                expected_stage="ready",
                expected_intent_code="AG_TRANS",
                expected_slots={"amount": "200", "payee_name": "小明"},
                expected_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_required_slots={"payee_name": "小明", "amount": "200"},
    ),
    DialogScenario(
        name="three_turn_name_card_then_amount",
        description="第一轮只表达转账意图，第二轮补姓名和卡号，第三轮补金额。",
        turns=(
            TurnExpectation(
                user_input="帮我转账",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={},
                expected_reply="请提供金额、收款人姓名",
            ),
            TurnExpectation(
                user_input="收款人王芳，收款卡号6222020100043219999",
                expected_stage="asking",
                expected_intent_code="AG_TRANS",
                expected_slots={
                    "payee_name": "王芳",
                    "payee_card_no": "6222020100043219999",
                },
                expected_reply="请提供金额",
            ),
            TurnExpectation(
                user_input="转500元",
                expected_stage="ready",
                expected_intent_code="AG_TRANS",
                expected_slots={
                    "payee_name": "王芳",
                    "payee_card_no": "6222020100043219999",
                    "amount": "500",
                },
                expected_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_required_slots={
            "payee_name": "王芳",
            "payee_card_no": "6222020100043219999",
            "amount": "500",
        },
    ),
)

SCENARIOS_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
    host_header: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Send one JSON request and decode the JSON response."""
    headers = {"Content-Type": "application/json"}
    if host_header:
        headers["Host"] = host_header
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout calling {url} after {timeout_seconds:.0f}s") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"transport error calling {url}: {exc.reason}") from exc


def _create_session(
    *,
    base_url: str,
    cust_id: str,
    host_header: str | None,
    timeout_seconds: float,
) -> str:
    """Create one router session and return the session id."""
    payload = _request_json(
        method="POST",
        url=f"{base_url.rstrip('/')}/api/router/v2/sessions",
        payload={"cust_id": cust_id},
        host_header=host_header,
        timeout_seconds=timeout_seconds,
    )
    return str(payload["session_id"])


def _send_turn(
    *,
    base_url: str,
    session_id: str,
    cust_id: str,
    content: str,
    host_header: str | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Send one user message turn through the real router HTTP API."""
    payload = _request_json(
        method="POST",
        url=f"{base_url.rstrip('/')}/api/router/v2/sessions/{session_id}/messages",
        payload={
            "cust_id": cust_id,
            "content": content,
            "executionMode": "router_only",
        },
        host_header=host_header,
        timeout_seconds=timeout_seconds,
    )
    return payload["snapshot"]


def _last_assistant_reply(snapshot: dict[str, Any]) -> str:
    """Return the latest assistant reply from the session transcript."""
    messages = snapshot.get("messages") or []
    for item in reversed(messages):
        if item.get("role") == "assistant":
            return str(item.get("content", ""))
    return ""


def _active_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the active router payload for the current turn."""
    pending = snapshot.get("pending_graph")
    if isinstance(pending, dict):
        return pending
    current = snapshot.get("current_graph")
    if isinstance(current, dict):
        return current
    return {}


def _current_item(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the first current intent item from the active payload."""
    payload = _active_payload(snapshot)
    items = payload.get("nodes") or []
    for item in items:
        if isinstance(item, dict):
            return item
    return {}


def _stage(snapshot: dict[str, Any]) -> str:
    """Project the raw router status into a simple dialog stage."""
    raw_status = str(_active_payload(snapshot).get("status") or "").strip()
    if raw_status in {"waiting_user_input", "waiting_confirmation_node"}:
        return "asking"
    if raw_status == "waiting_confirmation":
        return "confirming"
    if raw_status == "ready_for_dispatch":
        return "ready"
    if raw_status in {"completed", "partially_completed"}:
        return "done"
    return "idle"


def _dialog_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project one raw router snapshot into a simple dialog result."""
    item = _current_item(snapshot)
    return {
        "assistant_reply": _last_assistant_reply(snapshot),
        "stage": _stage(snapshot),
        "intent_code": str(item.get("intent_code", "")),
        "slots": dict(item.get("slot_memory") or {}),
    }


def _compare_turn(*, actual: dict[str, Any], expected: TurnExpectation) -> list[str]:
    """Compare one live dialog turn against one fixed expectation."""
    mismatches: list[str] = []
    if actual["stage"] != expected.expected_stage:
        mismatches.append(
            f"stage mismatch: expected={expected.expected_stage!r}, actual={actual['stage']!r}"
        )
    if actual["intent_code"] != expected.expected_intent_code:
        mismatches.append(
            f"intent mismatch: expected={expected.expected_intent_code!r}, actual={actual['intent_code']!r}"
        )
    if actual["slots"] != expected.expected_slots:
        mismatches.append(
            f"slots mismatch: expected={expected.expected_slots!r}, actual={actual['slots']!r}"
        )
    if actual["assistant_reply"] != expected.expected_reply:
        mismatches.append(
            f"assistant_reply mismatch: expected={expected.expected_reply!r}, actual={actual['assistant_reply']!r}"
        )
    return mismatches


def _print_turn_result(*, turn_index: int, content: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Print one turn result in a compact JSON block and return it."""
    result = {
        "turn_index": turn_index,
        "user_input": content,
        **_dialog_result(snapshot),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _run_interactive_dialog(
    *,
    base_url: str,
    session_id: str,
    cust_id: str,
    host_header: str | None,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Read terminal input turn by turn and send it to the live router."""
    outputs: list[dict[str, Any]] = []
    print(f"session_id: {session_id}")
    print("输入一轮用户话术后回车发送，空行或 quit 结束。")
    turn_index = 1
    while True:
        try:
            content = input("你: ").strip()
        except EOFError:
            break
        if not content or content.lower() in {"quit", "exit"}:
            break
        snapshot = _send_turn(
            base_url=base_url,
            session_id=session_id,
            cust_id=cust_id,
            content=content,
            host_header=host_header,
            timeout_seconds=timeout_seconds,
        )
        outputs.append(
            _print_turn_result(
                turn_index=turn_index,
                content=content,
                snapshot=snapshot,
            )
        )
        turn_index += 1
    return outputs


def _run_scenario(
    *,
    base_url: str,
    cust_id: str,
    host_header: str | None,
    timeout_seconds: float,
    scenario: DialogScenario,
) -> dict[str, Any]:
    """Run one fixed dialog scenario and return the verification report."""
    try:
        session_id = _create_session(
            base_url=base_url,
            cust_id=cust_id,
            host_header=host_header,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        report = {
            "scenario": scenario.name,
            "description": scenario.description,
            "session_id": None,
            "passed": False,
            "turns": [],
            "final_required_slots": scenario.final_required_slots,
            "final_actual_slots": {},
            "mismatches": [f"create session failed: {exc}"],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    results: list[dict[str, Any]] = []
    all_mismatches: list[str] = []
    last_actual_slots: dict[str, Any] = {}

    for turn_index, expectation in enumerate(scenario.turns, start=1):
        try:
            snapshot = _send_turn(
                base_url=base_url,
                session_id=session_id,
                cust_id=cust_id,
                content=expectation.user_input,
                host_header=host_header,
                timeout_seconds=timeout_seconds,
            )
            actual = {
                "turn_index": turn_index,
                "user_input": expectation.user_input,
                **_dialog_result(snapshot),
            }
            mismatches = _compare_turn(actual=actual, expected=expectation)
            last_actual_slots = dict(actual["slots"])
        except Exception as exc:
            actual = {
                "turn_index": turn_index,
                "user_input": expectation.user_input,
                "assistant_reply": "",
                "stage": "transport_error",
                "intent_code": "",
                "slots": {},
            }
            mismatches = [f"request failed: {exc}"]
        actual["passed"] = not mismatches
        actual["mismatches"] = mismatches
        results.append(actual)
        all_mismatches.extend(f"turn {turn_index}: {message}" for message in mismatches)
        if mismatches and actual["stage"] == "transport_error":
            break

    final_slot_mismatches: list[str] = []
    for slot_key, expected_value in scenario.final_required_slots.items():
        actual_value = last_actual_slots.get(slot_key)
        if actual_value != expected_value:
            final_slot_mismatches.append(
                f"final slot {slot_key!r} mismatch: expected={expected_value!r}, actual={actual_value!r}"
            )
    all_mismatches.extend(final_slot_mismatches)
    passed = not all_mismatches

    report = {
        "scenario": scenario.name,
        "description": scenario.description,
        "session_id": session_id,
        "passed": passed,
        "turns": results,
        "final_required_slots": scenario.final_required_slots,
        "final_actual_slots": last_actual_slots,
        "mismatches": all_mismatches,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _resolve_scenarios(selected_names: list[str] | None) -> list[DialogScenario]:
    """Resolve the target scenarios selected from CLI."""
    if not selected_names:
        return list(SCENARIOS)
    missing = [name for name in selected_names if name not in SCENARIOS_BY_NAME]
    if missing:
        raise SystemExit(f"unknown scenarios: {', '.join(missing)}")
    return [SCENARIOS_BY_NAME[name] for name in selected_names]


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Verify real router multi-turn intent+slot scenarios.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Router base URL.")
    parser.add_argument("--host-header", default=DEFAULT_HOST_HEADER, help="Optional Host header for ingress routing.")
    parser.add_argument("--cust-id", default=DEFAULT_CUST_ID, help="cust_id used to create test sessions.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout per request.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=DEFAULT_INTERACTIVE,
        help="Run manual turn-by-turn dialog mode.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Scenario name to run. Can be passed multiple times. Defaults to all built-in scenarios in non-interactive mode.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="Print built-in scenario names and exit.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the dialog check in interactive mode or scenario-suite mode."""
    args = _parse_args()
    if args.list_scenarios:
        print(
            json.dumps(
                [
                    {"name": scenario.name, "description": scenario.description, "turn_count": len(scenario.turns)}
                    for scenario in SCENARIOS
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    base_url = args.base_url.rstrip("/")
    if args.interactive:
        session_id = _create_session(
            base_url=base_url,
            cust_id=args.cust_id,
            host_header=args.host_header,
            timeout_seconds=args.timeout_seconds,
        )
        outputs = _run_interactive_dialog(
            base_url=base_url,
            session_id=session_id,
            cust_id=args.cust_id,
            host_header=args.host_header,
            timeout_seconds=args.timeout_seconds,
        )
        print(
            json.dumps(
                {
                    "mode": "interactive",
                    "session_id": session_id,
                    "turns": outputs,
                    "final_result": outputs[-1] if outputs else {},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    reports = [
        _run_scenario(
            base_url=base_url,
            cust_id=args.cust_id,
            host_header=args.host_header,
            timeout_seconds=args.timeout_seconds,
            scenario=scenario,
        )
        for scenario in _resolve_scenarios(args.scenario)
    ]
    summary = {
        "mode": "scenario_suite",
        "scenario_count": len(reports),
        "passed_count": sum(1 for report in reports if report["passed"]),
        "failed_scenarios": [report["scenario"] for report in reports if not report["passed"]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passed_count"] == summary["scenario_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
