#!/usr/bin/env python3
"""Call router-only mode to inspect intent recognition and slot filling."""

from __future__ import annotations

import argparse
import json
import time
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
    parser.add_argument("--cust-id", default="cust_demo", help="custId used in the router request payload.")
    parser.add_argument(
        "--message",
        default="帮我查一下余额，如果大于1000，就给小红转200，如果还大于1000，就再给小明转200",
        help="Message to route.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    session_id = f"verify_understanding_{int(time.time() * 1000)}"

    status, body = _request(
        method="POST",
        url=f"{base_url}/api/v1/message",
        payload={
            "sessionId": session_id,
            "txt": args.message,
            "custId": args.cust_id,
            "executionMode": "router_only",
            "stream": False,
            "config_variables": [
                {"name": "custID", "value": args.cust_id},
                {"name": "sessionID", "value": session_id},
                {"name": "currentDisplay", "value": "router_only_debug"},
                {"name": "agentSessionID", "value": session_id},
            ],
        },
        host_header=args.host_header,
    )
    if status != 200:
        raise RuntimeError(f"route failed: status={status}, body={body}")

    payload = json.loads(body)
    print(
        json.dumps(
            {
                "session_id": session_id,
                "status": payload.get("status"),
                "assistant_reply": payload.get("message"),
                "intent_code": payload.get("intent_code"),
                "slot_memory": payload.get("slot_memory") or {},
                "output": payload.get("output") or {},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
