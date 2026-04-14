#!/usr/bin/env python3
"""Call the router analyze-only API and print intent plus slot results."""

from __future__ import annotations

import json
from typing import Any
import urllib.error
import urllib.request
from uuid import uuid4


BASE_URL = "http://intent-router.kkrrc-359.top"
CUST_ID = "cust_demo"
MESSAGE = "帮我查一下余额，如果大于1000，就给小红转200，如果还大于1000，就再给小明转200"
PRINT_RAW_RESPONSE = False


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send one JSON request and return the decoded JSON response body."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


def _build_summary(response: dict[str, Any]) -> dict[str, Any]:
    """Extract the intent and slot-focused view from the analyze response."""
    analysis = response.get("analysis") or {}
    return {
        "session_id": analysis.get("session_id"),
        "cust_id": analysis.get("cust_id"),
        "content": analysis.get("content"),
        "no_match": analysis.get("no_match"),
        "primary_intents": (analysis.get("recognition") or {}).get("primary", []),
        "candidate_intents": (analysis.get("recognition") or {}).get("candidates", []),
        "slot_nodes": [
            {
                "node_id": node.get("node_id"),
                "intent_code": node.get("intent_code"),
                "title": node.get("title"),
                "confidence": node.get("confidence"),
                "source_fragment": node.get("source_fragment"),
                "slot_memory": node.get("slot_memory", {}),
                "slot_bindings": node.get("slot_bindings", []),
            }
            for node in analysis.get("slot_nodes", [])
        ],
        "conditional_edges": analysis.get("conditional_edges", []),
    }


def main() -> int:
    """Call the analyze endpoint with the constants defined at the top of this file."""
    session_id = f"analyze-{uuid4().hex}"
    url = f"{BASE_URL.rstrip('/')}/api/router/v2/sessions/{session_id}/messages/analyze"
    payload = {
        "cust_id": CUST_ID,
        "content": MESSAGE,
    }
    response = _request_json(method="POST", url=url, payload=payload)
    output = response if PRINT_RAW_RESPONSE else _build_summary(response)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
