#!/usr/bin/env python3
"""Run focused `/api/v1/message` regression scenarios against a live router endpoint."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from time import perf_counter, time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://intent-router.kkrrc-359.top"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_CUST_ID = "C0001"


def _request_json(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connection": "close",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


def _message_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/message"


def _payload(
    *,
    session_id: str,
    txt: str,
    execution_mode: str,
    current_display: str,
    cust_id: str,
) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "txt": txt,
        "stream": False,
        "executionMode": execution_mode,
        "custId": cust_id,
        "config_variables": [
            {"name": "custID", "value": cust_id},
            {"name": "sessionID", "value": session_id},
            {"name": "currentDisplay", "value": current_display},
            {"name": "agentSessionID", "value": session_id},
        ],
    }


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_slot_subset(body: dict[str, Any], expected: dict[str, str]) -> None:
    slot_memory = body.get("slot_memory") or {}
    _expect(isinstance(slot_memory, dict), f"slot_memory is not a dict: {slot_memory!r}")
    for slot_key, expected_value in expected.items():
        actual_value = str(slot_memory.get(slot_key))
        _expect(
            actual_value == expected_value,
            f"slot {slot_key} expected {expected_value!r}, got {actual_value!r}",
        )


@dataclass(frozen=True)
class TurnExpectation:
    txt: str
    status: str
    expected_slots: dict[str, str]
    completion_state: int | None = None
    message_contains: str | None = None


@dataclass(frozen=True)
class Scenario:
    case_id: str
    execution_mode: str
    turns: tuple[TurnExpectation, ...]


def _run_scenario(
    *,
    scenario: Scenario,
    base_url: str,
    timeout_seconds: float,
    cust_id: str,
) -> dict[str, Any]:
    started_at = perf_counter()
    session_id = f"router_v1_regression_{scenario.case_id}_{int(time() * 1000)}"
    step_results: list[dict[str, Any]] = []

    try:
        for index, turn in enumerate(scenario.turns, start=1):
            body = _request_json(
                url=_message_url(base_url),
                payload=_payload(
                    session_id=session_id,
                    txt=turn.txt,
                    execution_mode=scenario.execution_mode,
                    current_display="transfer_page" if index < len(scenario.turns) else "transfer_confirm_page",
                    cust_id=cust_id,
                ),
                timeout_seconds=timeout_seconds,
            )
            _expect(body.get("ok") is True, f"turn {index} returned ok={body.get('ok')!r}")
            _expect(body.get("status") == turn.status, f"turn {index} status mismatch: {body.get('status')!r}")
            _assert_slot_subset(body, turn.expected_slots)
            if turn.completion_state is not None:
                _expect(
                    body.get("completion_state") == turn.completion_state,
                    f"turn {index} completion_state mismatch: {body.get('completion_state')!r}",
                )
            if turn.message_contains is not None:
                message = str(body.get("message") or "")
                _expect(
                    turn.message_contains in message,
                    f"turn {index} message mismatch: expected substring {turn.message_contains!r}, got {message!r}",
                )
            step_results.append(
                {
                    "turn": index,
                    "txt": turn.txt,
                    "status": body.get("status"),
                    "completion_state": body.get("completion_state"),
                    "slot_memory": body.get("slot_memory"),
                    "message": body.get("message"),
                }
            )
        return {
            "case_id": scenario.case_id,
            "execution_mode": scenario.execution_mode,
            "passed": True,
            "elapsed_ms": round((perf_counter() - started_at) * 1000, 2),
            "session_id": session_id,
            "steps": step_results,
        }
    except Exception as exc:
        return {
            "case_id": scenario.case_id,
            "execution_mode": scenario.execution_mode,
            "passed": False,
            "elapsed_ms": round((perf_counter() - started_at) * 1000, 2),
            "session_id": session_id,
            "steps": step_results,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            case_id="single_turn_basic_transfer",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="给小红转200",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="single_turn_named_transfer",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="给王芳转300",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "王芳", "amount": "300"},
                ),
            ),
        ),
        Scenario(
            case_id="multi_turn_name_then_amount",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="小红",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小红"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="200",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="multi_turn_amount_then_name",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="200",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"amount": "200"},
                    message_contains="收款人",
                ),
                TurnExpectation(
                    txt="小红",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="multi_turn_fill_all_missing_on_second_turn",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="给小红转200",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="multi_turn_named_first_turn_then_amount",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="我要给小红转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小红"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="200",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="multi_turn_amount_first_turn_then_name",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="我要转账200",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"amount": "200"},
                    message_contains="收款人",
                ),
                TurnExpectation(
                    txt="小红",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="multi_turn_override_payee_before_amount",
            execution_mode="execute",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="小刚",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小刚"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="小红吧",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小红"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="200",
                    status="completed",
                    completion_state=2,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="router_only_name_then_amount",
            execution_mode="router_only",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="小红",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小红"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="200",
                    status="ready_for_dispatch",
                    completion_state=0,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="router_only_amount_then_name",
            execution_mode="router_only",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="200",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"amount": "200"},
                    message_contains="收款人",
                ),
                TurnExpectation(
                    txt="小红",
                    status="ready_for_dispatch",
                    completion_state=0,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="router_only_fill_all_missing_on_second_turn",
            execution_mode="router_only",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="给小红转200",
                    status="ready_for_dispatch",
                    completion_state=0,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
        Scenario(
            case_id="router_only_override_payee_before_amount",
            execution_mode="router_only",
            turns=(
                TurnExpectation(
                    txt="我要转账",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={},
                    message_contains="请提供",
                ),
                TurnExpectation(
                    txt="小刚",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小刚"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="小红吧",
                    status="waiting_user_input",
                    completion_state=0,
                    expected_slots={"payee_name": "小红"},
                    message_contains="金额",
                ),
                TurnExpectation(
                    txt="200",
                    status="ready_for_dispatch",
                    completion_state=0,
                    expected_slots={"payee_name": "小红", "amount": "200"},
                ),
            ),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run focused `/api/v1/message` regression scenarios.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Router base URL. Default: {DEFAULT_BASE_URL}")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-request timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument("--cust-id", default=DEFAULT_CUST_ID, help=f"custId. Default: {DEFAULT_CUST_ID}")
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Run only the selected case id. Repeatable.",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="List built-in case ids and exit.",
    )
    args = parser.parse_args()

    scenarios = _scenarios()
    if args.list_cases:
        print(json.dumps([scenario.case_id for scenario in scenarios], ensure_ascii=False, indent=2))
        return 0
    selected_case_ids = {item.strip() for item in args.case_id if item.strip()}
    if selected_case_ids:
        scenarios = tuple(
            scenario for scenario in scenarios if scenario.case_id in selected_case_ids
        )
        if not scenarios:
            raise SystemExit(f"no scenarios matched case ids: {sorted(selected_case_ids)!r}")

    results = [
        _run_scenario(
            scenario=scenario,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            cust_id=args.cust_id,
        )
        for scenario in scenarios
    ]
    passed = sum(1 for item in results if item["passed"])
    payload = {
        "base_url": args.base_url,
        "timeout_seconds": args.timeout_seconds,
        "cust_id": args.cust_id,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
