#!/usr/bin/env python3
"""Call the router analyze API in intent-only mode and print recognition results."""

from __future__ import annotations

import json
from typing import Any
import urllib.error
import urllib.request
from uuid import uuid4


BASE_URL = "http://intent-router.kkrrc-359.top"
CUST_ID = "cust_demo"
MESSAGE = "给小红转200"


def _request_json(*, method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSON request and decode the JSON response."""
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


def _intent_view(response: dict[str, Any]) -> dict[str, Any]:
    """Keep only the intent recognition fields needed for intent-only validation."""
    analysis = response.get("analysis") or {}
    recognition = analysis.get("recognition") or {}
    return {
        "session_id": analysis.get("session_id"),
        "cust_id": analysis.get("cust_id"),
        "content": analysis.get("content"),
        "no_match": analysis.get("no_match"),
        "primary_intents": recognition.get("primary", []),
        "candidate_intents": recognition.get("candidates", []),
    }


def main() -> int:
    """Run one analyze-only intent-recognition request and print the result."""
    session_id = f"intent-only-{uuid4().hex}"
    url = f"{BASE_URL.rstrip('/')}/api/router/v2/sessions/{session_id}/messages/analyze"
    payload = {
        "cust_id": CUST_ID,
        "content": MESSAGE,
        "analysisMode": "intent_only",
    }
    response = _request_json(method="POST", url=url, payload=payload)
    print(json.dumps(_intent_view(response), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
