#!/usr/bin/env python3
"""Send one multi-turn dialog to router-service and print the dialog result only."""

from __future__ import annotations

import json
import os
from typing import Any
import urllib.error
import urllib.request


BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://intent-router.kkrrc-359.top")
HOST_HEADER = os.getenv("INTENT_ROUTER_HOST_HEADER")
CUST_ID = os.getenv("INTENT_ROUTER_CUST_ID", "cust_demo")
TIMEOUT_SECONDS = float(os.getenv("INTENT_ROUTER_TIMEOUT_SECONDS", "90"))

# 直接在这里改测试对话，不需要额外 JSON 文件。
TURNS = [
    "帮我转账",
    "收款人王芳，收款卡号6222020100043219999",
    "转500元",
]


def _request_json(*, method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON request and decode the JSON response."""
    headers = {"Content-Type": "application/json"}
    if HOST_HEADER:
        headers["Host"] = HOST_HEADER
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


def _create_session() -> str:
    """Create one router session and return the session id."""
    payload = _request_json(
        method="POST",
        url=f"{BASE_URL.rstrip('/')}/api/router/v2/sessions",
        payload={"cust_id": CUST_ID},
    )
    return str(payload["session_id"])


def _send_turn(*, session_id: str, content: str) -> dict[str, Any]:
    """Send one user message turn through the real router HTTP API."""
    payload = _request_json(
        method="POST",
        url=f"{BASE_URL.rstrip('/')}/api/router/v2/sessions/{session_id}/messages",
        payload={
            "cust_id": CUST_ID,
            "content": content,
            "executionMode": "router_only",
        },
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


def main() -> int:
    """Run the built-in dialog turns and print each turn result."""
    session_id = _create_session()
    outputs: list[dict[str, Any]] = []
    for index, content in enumerate(TURNS, start=1):
        snapshot = _send_turn(session_id=session_id, content=content)
        outputs.append(
            {
                "turn_index": index,
                "user_input": content,
                **_dialog_result(snapshot),
            }
        )
    print(
        json.dumps(
            {
                "session_id": session_id,
                "turns": outputs,
                "final_result": outputs[-1] if outputs else {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
