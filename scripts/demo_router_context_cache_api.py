#!/usr/bin/env python3
"""Real router short-context cache demo.

This script calls router-service directly and proves two things:
1. The current task keeps short-term slot memory across turns.
2. After the task is completed, Router releases the graph/task state while
   keeping the merged slots in session-level shared_slot_memory.

Direct run:

    python scripts/demo_router_context_cache_api.py

Optional environment variables:

    INTENT_ROUTER_BASE_URL=http://intent-router.kkrrc-359.top
    INTENT_ROUTER_CUST_ID=C0001
    INTENT_ROUTER_EXECUTION_MODE=execute

Scenario:
1. Turn 1 sends "给小明转账" without slots_data.
2. Router extracts and caches payee_name=小明 in this session.
3. Turn 2 sends only "200" with the same sessionId, still without slots_data.
4. Router reuses cached payee_name and merges amount=200 into slot_memory.
5. If Router returns completion_state=1, this script calls /api/v1/task/completion.
6. The script reads the internal session snapshot only as a developer proof and
   asserts shared_slot_memory still contains payee_name=小明 and amount=200.
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
SESSION_ID = os.getenv("INTENT_ROUTER_SESSION_ID", f"context_cache_demo_{int(time.time() * 1000)}")

TURN_1_TEXT = "给小明转账"
TURN_2_TEXT = "200"
DISPLAY_1 = "transfer_page"
DISPLAY_2 = "transfer_confirm_page"


def _request_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def _message_payload(*, txt: str, current_display: str) -> dict[str, Any]:
    return {
        "sessionId": SESSION_ID,
        "txt": txt,
        "custId": CUST_ID,
        "executionMode": EXECUTION_MODE,
        "stream": False,
        "config_variables": [
            {"name": "custID", "value": CUST_ID},
            {"name": "sessionID", "value": SESSION_ID},
            {"name": "currentDisplay", "value": current_display},
            {"name": "agentSessionID", "value": SESSION_ID},
        ],
    }


def _completion_payload(*, task_id: str, completion_signal: int) -> dict[str, Any]:
    return {
        "sessionId": SESSION_ID,
        "taskId": task_id,
        "completionSignal": completion_signal,
    }


def _output(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("ok") is not True:
        raise AssertionError(f"expected ok=true, got: {response}")
    output = response.get("output")
    if not isinstance(output, dict):
        raise AssertionError(f"expected output object, got: {response}")
    slot_memory = output.get("slot_memory")
    if not isinstance(slot_memory, dict):
        raise AssertionError(f"expected output.slot_memory object, got: {response}")
    return output


def _print_block(title: str, payload: dict[str, Any]) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def _assert_context_cache(turn1_output: dict[str, Any], turn2_output: dict[str, Any]) -> None:
    turn1_slots = turn1_output["slot_memory"]
    turn2_slots = turn2_output["slot_memory"]

    if turn1_slots.get("payee_name") != "小明":
        raise AssertionError(f"turn1 did not cache payee_name=小明: {turn1_output}")
    if "amount" in turn1_slots:
        raise AssertionError(f"turn1 should not have amount before user provides it: {turn1_output}")
    if turn2_slots.get("payee_name") != "小明":
        raise AssertionError(f"turn2 did not reuse cached payee_name=小明: {turn2_output}")
    if str(turn2_slots.get("amount")) != "200":
        raise AssertionError(f"turn2 did not extract amount=200: {turn2_output}")


def _finalize_if_needed(*, base_url: str, turn2_output: dict[str, Any]) -> dict[str, Any] | None:
    completion_state = turn2_output.get("completion_state")
    status = turn2_output.get("status")
    if completion_state == 2 and status == "completed":
        return None
    if completion_state != 1 or status != "waiting_assistant_completion":
        raise AssertionError(
            "expected turn2 to be either completed or waiting for assistant completion, "
            f"got: {turn2_output}"
        )
    task_id = turn2_output.get("current_task")
    if not isinstance(task_id, str) or not task_id:
        raise AssertionError(f"turn2 current_task is required for completion callback: {turn2_output}")

    request = _completion_payload(task_id=task_id, completion_signal=1)
    response = _request_json("POST", f"{base_url}/api/v1/task/completion", request)
    output = _output(response)
    if output.get("completion_state") != 2 or output.get("status") != "completed":
        raise AssertionError(f"completion callback did not finalize the task: {response}")
    _print_block("completion.request", request)
    _print_block("completion.response", response)
    return output


def _assert_session_shared_memory(snapshot: dict[str, Any]) -> None:
    shared = snapshot.get("shared_slot_memory")
    if not isinstance(shared, dict):
        raise AssertionError(f"snapshot.shared_slot_memory must be an object: {snapshot}")
    if shared.get("payee_name") != "小明":
        raise AssertionError(f"session shared_slot_memory did not keep payee_name=小明: {snapshot}")
    if str(shared.get("amount")) != "200":
        raise AssertionError(f"session shared_slot_memory did not keep amount=200: {snapshot}")
    if snapshot.get("current_graph") is not None:
        raise AssertionError(f"current_graph should be released after completion: {snapshot}")
    if snapshot.get("pending_graph") is not None:
        raise AssertionError(f"pending_graph should be empty after completion: {snapshot}")


def main() -> int:
    base_url = BASE_URL.rstrip("/")
    print("=== real router context-cache config ===")
    print(
        json.dumps(
            {
                "base_url": base_url,
                "session_id": SESSION_ID,
                "cust_id": CUST_ID,
                "execution_mode": EXECUTION_MODE,
                "turn_1": TURN_1_TEXT,
                "turn_2": TURN_2_TEXT,
                "slots_data_from_upstream": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print()

    turn1_request = _message_payload(txt=TURN_1_TEXT, current_display=DISPLAY_1)
    turn1_response = _request_json("POST", f"{base_url}/api/v1/message", turn1_request)
    turn1_output = _output(turn1_response)

    turn2_request = _message_payload(txt=TURN_2_TEXT, current_display=DISPLAY_2)
    turn2_response = _request_json("POST", f"{base_url}/api/v1/message", turn2_request)
    turn2_output = _output(turn2_response)

    _assert_context_cache(turn1_output, turn2_output)
    completion_output = _finalize_if_needed(base_url=base_url, turn2_output=turn2_output)
    session_snapshot = _request_json("GET", f"{base_url}/api/router/v2/sessions/{SESSION_ID}")
    _assert_session_shared_memory(session_snapshot)

    _print_block("turn1.request", turn1_request)
    _print_block("turn1.response", turn1_response)
    _print_block("turn2.request", turn2_request)
    _print_block("turn2.response", turn2_response)
    _print_block("session.snapshot.after_completion", session_snapshot)
    _print_block(
        "context_cache.proof",
        {
            "same_session_id": SESSION_ID,
            "upstream_did_not_send_slots_data": True,
            "turn1_cached": turn1_output["slot_memory"],
            "turn2_reused_and_merged": turn2_output["slot_memory"],
            "completion_output": completion_output,
            "session_shared_slot_memory_after_task_release": session_snapshot["shared_slot_memory"],
            "current_graph_after_completion": session_snapshot["current_graph"],
            "pending_graph_after_completion": session_snapshot["pending_graph"],
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
