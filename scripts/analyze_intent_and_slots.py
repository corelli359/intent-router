#!/usr/bin/env python3
"""Call the router analyze-only API and print intent plus slot results."""

from __future__ import annotations

import argparse
import json
from typing import Any
import urllib.error
import urllib.request
from uuid import uuid4


DEFAULT_BASE_URL = "http://intent-router.kkrrc-359.top"
DEFAULT_MESSAGE = "帮我查一下余额，如果大于1000，就给小红转200，如果还大于1000，就再给小明转200"


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
    """Parse args, call the router analyze endpoint, and print the result."""
    parser = argparse.ArgumentParser(
        description="Analyze one message through the router and return intent plus slot results only."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Router ingress base URL.")
    parser.add_argument("--cust-id", default="cust_demo", help="Customer id sent to the router.")
    parser.add_argument("--session-id", default=None, help="Optional session id. Auto-generated when omitted.")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="User message to analyze.")
    parser.add_argument(
        "--print-raw",
        action="store_true",
        help="Print the full raw API response instead of the intent-and-slot summary.",
    )
    args = parser.parse_args()

    session_id = args.session_id or f"analyze-{uuid4().hex}"
    base_url = args.base_url.rstrip("/")
    url = f"{base_url}/api/router/v2/sessions/{session_id}/messages/analyze"
    payload = {
        "cust_id": args.cust_id,
        "content": args.message,
    }
    response = _request_json(method="POST", url=url, payload=payload)
    output = response if args.print_raw else _build_summary(response)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
