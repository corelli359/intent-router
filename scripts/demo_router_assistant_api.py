#!/usr/bin/env python3
"""Minimal assistant-to-router API demo.

Run directly without arguments:

    python scripts/demo_router_assistant_api.py

This script is intentionally simple so another service team can copy the file
and adapt the HTTP calls in their own program.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


BASE_URL = "http://127.0.0.1:8012"
CUST_ID = "C0001"
EXECUTION_MODE = "execute"  # "execute" or "router_only"
TURN_1_TEXT = "给小明转账"
TURN_2_TEXT = "200"
DISPLAY_1 = "transfer_page"
DISPLAY_2 = "transfer_confirm_page"


def request_json(method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc


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


def assert_assistant_response_shape(response: dict[str, Any]) -> None:
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


def main() -> int:
    session_id = f"assistant_session_demo_{int(time.time())}"

    create_request = {
        "session_id": session_id,
        "cust_id": CUST_ID,
    }
    create_response = request_json(
        "POST",
        f"{BASE_URL}/api/router/v2/sessions",
        create_request,
    )
    print_block("session.create.request", create_request)
    print_block("session.create.response", create_response)

    turn1_request = assistant_message_payload(
        txt=TURN_1_TEXT,
        session_id=session_id,
        current_display=DISPLAY_1,
    )
    turn1_response = request_json(
        "POST",
        f"{BASE_URL}/api/v1/message",
        turn1_request,
    )
    assert_assistant_response_shape(turn1_response)
    print_block("turn1.request", turn1_request)
    print_block("turn1.response", turn1_response)

    turn2_request = assistant_message_payload(
        txt=TURN_2_TEXT,
        session_id=session_id,
        current_display=DISPLAY_2,
    )
    turn2_response = request_json(
        "POST",
        f"{BASE_URL}/api/v1/message",
        turn2_request,
    )
    assert_assistant_response_shape(turn2_response)
    print_block("turn2.request", turn2_request)
    print_block("turn2.response", turn2_response)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
