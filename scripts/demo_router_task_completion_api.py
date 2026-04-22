#!/usr/bin/env python3
"""Real router task-completion chain demo.

This script calls a deployed router-service directly. It does not embed an app,
does not import test doubles, and does not use any mock agent.

Direct run:

    python scripts/demo_router_task_completion_api.py

Optional environment variables:

    INTENT_ROUTER_BASE_URL=http://127.0.0.1:8000
    INTENT_ROUTER_CUST_ID=C0001
    INTENT_ROUTER_EXECUTION_MODE=execute

What it does for each scenario:
1. POST /api/v1/message           -> "给小明转账"
2. POST /api/v1/message           -> "200"
3. POST /api/v1/task/completion   -> assistant completionSignal=1 or 2

Expected business behavior for the canonical transfer flow:
- turn1 returns waiting_user_input
- turn2 may be either:
  - waiting_assistant_completion with completion_state=1
  - completed with completion_state=2
- callback is sent only when turn2 returns completion_state=1
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://intent-router.kkrrc-359.top").strip()
CUST_ID = os.getenv("INTENT_ROUTER_CUST_ID", "C0001")
EXECUTION_MODE = os.getenv("INTENT_ROUTER_EXECUTION_MODE", "execute")
TURN_1_TEXT = "给小明转账"
TURN_2_TEXT = "200"
DISPLAY_1 = "transfer_page"
DISPLAY_2 = "transfer_confirm_page"


def _request_json(method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Failed to call "
            f"{url}: {exc}\n"
            "Router is probably not running on this address.\n"
            "Set INTENT_ROUTER_BASE_URL or make sure router-service is reachable."
        ) from exc


def print_block(title: str, payload: dict[str, Any]) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def assistant_message_payload(*, session_id: str, txt: str, current_display: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "txt": txt,
        "custId": CUST_ID,
        "executionMode": EXECUTION_MODE,
        "stream": False,
        "config_variables": [
            {"name": "custID", "value": CUST_ID},
            {"name": "sessionID", "value": session_id},
            {"name": "currentDisplay", "value": current_display},
            {"name": "agentSessionID", "value": session_id},
        ],
    }


def completion_payload(*, session_id: str, task_id: str, completion_signal: int) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "taskId": task_id,
        "completionSignal": completion_signal,
    }


def assert_output_shape(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AssertionError(f"response must be a JSON object: {response!r}")
    if not isinstance(response.get("ok"), bool):
        raise AssertionError(f"response requires top-level boolean ok: {response}")
    output = response.get("output")
    if not isinstance(output, dict):
        raise AssertionError(f"response requires top-level object output: {response}")
    required_fields = [
        "current_task",
        "task_list",
        "completion_state",
        "completion_reason",
        "node_id",
        "intent_code",
        "status",
        "isHandOver",
        "handOverReason",
        "message",
        "data",
        "slot_memory",
    ]
    missing = [field for field in required_fields if field not in output]
    if missing:
        raise AssertionError(f"response output missing fields {missing}: {response}")
    return output


def _assert_turn_1(output: dict[str, Any], response: dict[str, Any]) -> None:
    if output["completion_state"] != 0:
        raise AssertionError(f"expected turn1 completion_state=0, got: {response}")
    if output["status"] != "waiting_user_input":
        raise AssertionError(f"expected turn1 waiting_user_input, got: {response}")


def _classify_turn_2(output: dict[str, Any], response: dict[str, Any]) -> str:
    if not isinstance(output["current_task"], str) or not output["current_task"]:
        raise AssertionError(f"expected non-empty current_task on turn2, got: {response}")
    if output["completion_state"] == 1 and output["status"] == "waiting_assistant_completion":
        return "assistant_callback_required"
    if output["completion_state"] == 2 and output["status"] == "completed":
        return "agent_final_done"
    raise AssertionError(
        "unexpected turn2 state for transfer flow, expected either "
        "(completion_state=1,status=waiting_assistant_completion) or "
        f"(completion_state=2,status=completed), got: {response}"
    )


def _assert_callback(
    *,
    completion_signal: int,
    output: dict[str, Any],
    response: dict[str, Any],
) -> None:
    if output["completion_state"] != 2:
        raise AssertionError(f"expected callback completion_state=2, got: {response}")
    if output["status"] != "completed":
        raise AssertionError(f"expected callback status=completed, got: {response}")
    expected_reason = "joint_done" if completion_signal == 1 else "assistant_final_done"
    if output["completion_reason"] != expected_reason:
        raise AssertionError(
            f"expected callback completion_reason={expected_reason}, got: {response}"
        )


def run_scenario(*, base_url: str, completion_signal: int) -> None:
    session_id = f"assistant_real_completion_{completion_signal}_{int(time.time() * 1000)}"
    turn1_request = assistant_message_payload(
        session_id=session_id,
        txt=TURN_1_TEXT,
        current_display=DISPLAY_1,
    )
    turn1_response = _request_json("POST", f"{base_url.rstrip('/')}/api/v1/message", turn1_request)
    turn1_output = assert_output_shape(turn1_response)
    _assert_turn_1(turn1_output, turn1_response)

    turn2_request = assistant_message_payload(
        session_id=session_id,
        txt=TURN_2_TEXT,
        current_display=DISPLAY_2,
    )
    turn2_response = _request_json("POST", f"{base_url.rstrip('/')}/api/v1/message", turn2_request)
    turn2_output = assert_output_shape(turn2_response)
    turn2_mode = _classify_turn_2(turn2_output, turn2_response)

    print_block(f"scenario[{completion_signal}].turn1.request", turn1_request)
    print_block(f"scenario[{completion_signal}].turn1.response", turn1_response)
    print_block(f"scenario[{completion_signal}].turn2.request", turn2_request)
    print_block(f"scenario[{completion_signal}].turn2.response", turn2_response)

    if turn2_mode == "agent_final_done":
        print_block(
            f"scenario[{completion_signal}].callback.skipped",
            {
                "reason": "turn2 already completed by agent",
                "completion_state": turn2_output["completion_state"],
                "completion_reason": turn2_output["completion_reason"],
                "status": turn2_output["status"],
            },
        )
        return

    callback_request = completion_payload(
        session_id=session_id,
        task_id=turn2_output["current_task"],
        completion_signal=completion_signal,
    )
    callback_response = _request_json(
        "POST",
        f"{base_url.rstrip('/')}/api/v1/task/completion",
        callback_request,
    )
    callback_output = assert_output_shape(callback_response)
    _assert_callback(
        completion_signal=completion_signal,
        output=callback_output,
        response=callback_response,
    )
    print_block(f"scenario[{completion_signal}].callback.request", callback_request)
    print_block(f"scenario[{completion_signal}].callback.response", callback_response)


def main() -> int:
    base_url = BASE_URL
    print("=== real router config ===")
    print(
        json.dumps(
            {
                "base_url": base_url,
                "cust_id": CUST_ID,
                "execution_mode": EXECUTION_MODE,
                "turn_1": TURN_1_TEXT,
                "turn_2": TURN_2_TEXT,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print()
    run_scenario(base_url=base_url, completion_signal=1)
    run_scenario(base_url=base_url, completion_signal=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
