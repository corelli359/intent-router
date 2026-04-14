#!/usr/bin/env python3
"""Call the router analyze API and print only intents plus slots."""

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


def _intent_slot_view(response: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields needed for intent and slot inspection."""
    analysis = response.get("analysis") or {}
    recognition = analysis.get("recognition") or {}
    return {
        "session_id": analysis.get("session_id"),
        "cust_id": analysis.get("cust_id"),
        "content": analysis.get("content"),
        "no_match": analysis.get("no_match"),
        "primary_intents": recognition.get("primary", []),
        "candidate_intents": recognition.get("candidates", []),
        "slots": [
            {
                "intent_code": node.get("intent_code"),
                "title": node.get("title"),
                "confidence": node.get("confidence"),
                "slot_memory": node.get("slot_memory", {}),
                "slot_bindings": node.get("slot_bindings", []),
            }
            for node in analysis.get("slot_nodes", [])
        ],
    }


def main() -> int:
    """Run one analyze-only request and print the intent-slot view."""
    session_id = f"intent-slots-{uuid4().hex}"
    url = f"{BASE_URL.rstrip('/')}/api/router/v2/sessions/{session_id}/messages/analyze"
    payload = {
        "cust_id": CUST_ID,
        "content": MESSAGE,
    }
    response = _request_json(method="POST", url=url, payload=payload)
    print(json.dumps(_intent_slot_view(response), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
