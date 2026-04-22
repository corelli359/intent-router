#!/usr/bin/env python3
"""Minimal mock assistant that calls router directly.

Run directly without arguments:

    python scripts/demo_router_assistant_api.py

This script uses only the assistant-facing production router endpoint:

    POST {INTENT_ROUTER_BASE_URL}/api/v1/message

It intentionally does not call any session-create API. The assistant side only
needs to keep one stable `sessionId` across turns. Router will bind and
continue the session by that id.

If you want to demo the assistant completion callback against a real router, use:

    python scripts/demo_router_task_completion_api.py

Reason:
That script now runs a real callback chain and demonstrates:
1. agent 1 + assistant 1 => 2
2. agent 1 + assistant 2 => 2
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL_CANDIDATES = (
    "http://127.0.0.1:8012",
    "http://127.0.0.1:8000",
)
BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "")
CUST_ID = os.getenv("INTENT_ROUTER_CUST_ID", "C0001")
EXECUTION_MODE = os.getenv("INTENT_ROUTER_EXECUTION_MODE", "execute")  # "execute" or "router_only"
SESSION_ID = os.getenv("INTENT_ROUTER_SESSION_ID", f"assistant_session_demo_{int(time.time())}")

# Single intent + slot fill demo:
# turn 1 expects Router to keep payee_name and ask for amount
# turn 2 expects Router to reuse payee_name and complete amount
TURN_1_TEXT = "给小明转账"
TURN_2_TEXT = "200"
DISPLAY_1 = "transfer_page"
DISPLAY_2 = "transfer_confirm_page"


def _health_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/health"


def _resolve_base_url() -> str:
    configured = BASE_URL.strip()
    if configured:
        return configured
    for candidate in DEFAULT_BASE_URL_CANDIDATES:
        request = urllib.request.Request(url=_health_url(candidate), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=1.5):
                return candidate
        except Exception:
            continue
    return DEFAULT_BASE_URL_CANDIDATES[0]


def request_json(method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Failed to call "
            f"{url}: {exc}\n"
            "Router is probably not running on this address.\n"
            "Local repo default is usually:\n"
            "  python -m uvicorn router_service.api.app:app --reload --port 8012\n"
            "or rerun with:\n"
            "  INTENT_ROUTER_BASE_URL=http://127.0.0.1:8012 python scripts/demo_router_assistant_api.py"
        ) from exc


def print_block(title: str, payload: dict[str, Any]) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def assistant_message_payload(
    *,
    txt: str,
    session_id: str,
    current_display: str,
) -> dict[str, Any]:
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


def assert_assistant_response_shape(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AssertionError(f"response must be a JSON object: {response!r}")
    if "snapshot" in response:
        raise AssertionError(f"assistant protocol must not return snapshot: {response}")
    if not isinstance(response.get("ok"), bool):
        raise AssertionError(f"assistant protocol requires top-level boolean ok: {response}")

    output = response.get("output")
    if not isinstance(output, dict):
        raise AssertionError(f"assistant protocol requires top-level object output: {response}")

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
        raise AssertionError(f"assistant output missing fields {missing}: {response}")
    if not isinstance(output["task_list"], list):
        raise AssertionError(f"assistant output task_list must be a list: {response}")
    if not isinstance(output["completion_state"], int):
        raise AssertionError(f"assistant output completion_state must be an int: {response}")

    return output


def print_turn_summary(turn_name: str, output: dict[str, Any]) -> None:
    summary = {
        "current_task": output.get("current_task"),
        "intent_code": output.get("intent_code"),
        "status": output.get("status"),
        "completion_state": output.get("completion_state"),
        "completion_reason": output.get("completion_reason"),
        "message": output.get("message"),
        "slot_memory": output.get("slot_memory"),
        "task_list": output.get("task_list"),
    }
    print_block(f"{turn_name}.summary", summary)


def main() -> int:
    resolved_base_url = _resolve_base_url()
    print("=== mock assistant config ===")
    print(json.dumps(
        {
            "base_url": resolved_base_url,
            "session_id": SESSION_ID,
            "cust_id": CUST_ID,
            "execution_mode": EXECUTION_MODE,
        },
        ensure_ascii=False,
        indent=2,
    ))
    print()

    turn1_request = assistant_message_payload(
        txt=TURN_1_TEXT,
        session_id=SESSION_ID,
        current_display=DISPLAY_1,
    )
    turn1_response = request_json(
        "POST",
        f"{resolved_base_url.rstrip('/')}/api/v1/message",
        turn1_request,
    )
    turn1_output = assert_assistant_response_shape(turn1_response)
    print_block("turn1.request", turn1_request)
    print_block("turn1.response", turn1_response)
    print_turn_summary("turn1", turn1_output)

    turn2_request = assistant_message_payload(
        txt=TURN_2_TEXT,
        session_id=SESSION_ID,
        current_display=DISPLAY_2,
    )
    turn2_response = request_json(
        "POST",
        f"{resolved_base_url.rstrip('/')}/api/v1/message",
        turn2_request,
    )
    turn2_output = assert_assistant_response_shape(turn2_response)
    print_block("turn2.request", turn2_request)
    print_block("turn2.response", turn2_response)
    print_turn_summary("turn2", turn2_output)

    print("=== expected reading ===")
    print("1. turn1 通常会进入 waiting_user_input，slot_memory 里先拿到收款人。")
    print("2. turn2 继续沿用同一个 sessionId，只传当前用户输入。")
    print("3. 如果短期记忆生效，turn2 的 slot_memory 里会同时看到收款人和金额。")
    print("4. execute 模式下，槽位齐全后会继续路由到下游 agent。")
    print("5. /api/v1/task/completion 的汇报接口演示，请使用 scripts/demo_router_task_completion_api.py。")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
