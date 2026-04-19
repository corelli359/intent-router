#!/usr/bin/env python3
"""Call router-only mode to inspect intent recognition and slot filling."""

from __future__ import annotations

import argparse
import json
import urllib.request
from typing import Any


def _request(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    host_header: str | None = None,
) -> tuple[int, str]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if host_header:
        headers["Host"] = host_header
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read().decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify router router-only mode.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Router base URL.")
    parser.add_argument("--host-header", default=None, help="Optional Host header for ingress routing.")
    parser.add_argument(
        "--message",
        default="帮我查一下余额，如果大于1000，就给小红转200，如果还大于1000，就再给小明转200",
        help="Message to route.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    status, body = _request(
        method="POST",
        url=f"{base_url}/api/router/v2/sessions",
        payload={},
        host_header=args.host_header,
    )
    if status != 201:
        raise RuntimeError(f"create session failed: status={status}, body={body}")
    session_id = json.loads(body)["session_id"]

    status, body = _request(
        method="POST",
        url=f"{base_url}/api/router/v2/sessions/{session_id}/messages",
        payload={
            "content": args.message,
            "executionMode": "router_only",
        },
        host_header=args.host_header,
    )
    if status != 200:
        raise RuntimeError(f"route failed: status={status}, body={body}")

    payload = json.loads(body)
    snapshot = payload.get("snapshot") or {}
    current_graph = snapshot.get("current_graph") or {}
    pending_graph = snapshot.get("pending_graph") or {}
    active_graph = current_graph if current_graph else pending_graph
    nodes = active_graph.get("nodes") or []

    print(
        json.dumps(
            {
                "session_id": session_id,
                "graph_status": active_graph.get("status"),
                "assistant_reply": ((snapshot.get("messages") or [{}])[-1]).get("content"),
                "intents": [node.get("intent_code") for node in nodes if isinstance(node, dict)],
                "slot_memory": (nodes[0].get("slot_memory") if nodes and isinstance(nodes[0], dict) else {}),
                "shared_slot_memory": snapshot.get("shared_slot_memory") or {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
