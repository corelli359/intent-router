#!/usr/bin/env python3
"""Call router analyze-only mode to inspect intent recognition and slot filling."""

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
    parser = argparse.ArgumentParser(description="Verify router analyze-only mode.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Router base URL.")
    parser.add_argument("--host-header", default=None, help="Optional Host header for ingress routing.")
    parser.add_argument(
        "--analysis-mode",
        choices=("full", "intent_only"),
        default="full",
        help="Whether to verify full understanding or intent recognition only.",
    )
    parser.add_argument(
        "--message",
        default="帮我查一下余额，如果大于1000，就给小红转200，如果还大于1000，就再给小明转200",
        help="Message to analyze.",
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
        url=f"{base_url}/api/router/v2/sessions/{session_id}/messages/analyze",
        payload={
            "content": args.message,
            "analysisMode": args.analysis_mode,
        },
        host_header=args.host_header,
    )
    if status != 200:
        raise RuntimeError(f"analyze failed: status={status}, body={body}")

    print(
        json.dumps(
            {
                "session_id": session_id,
                "analysis": json.loads(body).get("analysis"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
