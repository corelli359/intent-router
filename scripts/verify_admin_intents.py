#!/usr/bin/env python3
"""Lightweight admin intent API verifier for local MVP checks.

This script assumes an Admin API that exposes intent CRUD endpoints.
Paths are configurable so the script can be reused while endpoints evolve.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class HttpResult:
    status: int
    body: Any


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> HttpResult:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8").strip()
            parsed = json.loads(raw) if raw else {}
            return HttpResult(status=resp.status, body=parsed)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8").strip()
        parsed: Any
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return HttpResult(status=exc.code, body=parsed)


def _build_intent_payload(intent_code: str) -> dict[str, Any]:
    return {
        "intent_code": intent_code,
        "name": "Transfer Money",
        "description": "Handle transfer requests and slot collection.",
        "examples": ["transfer 200 to alex", "send money to li"],
        "agent_url": "http://127.0.0.1:9100/transfer/stream",
        "status": "active",
        "dispatch_priority": 10,
        "request_schema": {
            "type": "object",
            "required": ["sessionId", "taskId", "intentCode", "input"],
            "properties": {
                "sessionId": {"type": "string"},
                "taskId": {"type": "string"},
                "intentCode": {"type": "string"},
                "input": {"type": "string"},
                "context": {"type": "object"},
            },
        },
        "field_mapping": {
            "sessionId": "$session.id",
            "taskId": "$task.id",
            "intentCode": "$intent.code",
            "input": "$message.current",
        },
        "resume_policy": "resume_same_task",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify admin intent endpoints.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--create-path", default="/api/admin/intents")
    parser.add_argument("--list-path", default="/api/admin/intents")
    parser.add_argument("--update-path", default="/api/admin/intents/{intent_code}")
    parser.add_argument("--delete-path", default="/api/admin/intents/{intent_code}")
    parser.add_argument("--intent-code", default="transfer_money_mvp_check")
    parser.add_argument(
        "--full-crud",
        action="store_true",
        help="Also run update/delete checks after create/list.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail if list payload shape is unknown.",
    )
    return parser.parse_args()


def _url(base_url: str, path: str, intent_code: str | None = None) -> str:
    if intent_code is not None:
        path = path.format(intent_code=urllib.parse.quote(intent_code, safe=""))
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def main() -> int:
    args = parse_args()
    payload = _build_intent_payload(args.intent_code)

    create_result = _http_json("POST", _url(args.base_url, args.create_path), payload)
    if create_result.status not in (200, 201, 409):
        print(
            f"[FAIL] create intent status={create_result.status}, body={create_result.body}"
        )
        return 1
    print(f"[OK] create intent status={create_result.status}")

    list_result = _http_json("GET", _url(args.base_url, args.list_path))
    if list_result.status != 200:
        print(f"[FAIL] list intents status={list_result.status}, body={list_result.body}")
        return 1

    seen = False
    list_payload = list_result.body
    if isinstance(list_payload, list):
        seen = any(item.get("intent_code") == args.intent_code for item in list_payload)
    elif isinstance(list_payload, dict):
        for key in ("items", "data", "results"):
            if isinstance(list_payload.get(key), list):
                seen = any(
                    item.get("intent_code") == args.intent_code
                    for item in list_payload[key]
                    if isinstance(item, dict)
                )
                break
        if not seen and "intent_code" in list_payload:
            seen = list_payload.get("intent_code") == args.intent_code
    elif args.strict:
        print(f"[FAIL] list payload shape not recognized: {type(list_payload).__name__}")
        return 1

    if not seen:
        print(f"[WARN] created intent not found in list response: {list_payload}")
    else:
        print("[OK] created intent found in list response")

    if not args.full_crud:
        print("[DONE] validation finished (create/list)")
        return 0

    update_payload = dict(payload)
    update_payload["description"] = "Updated by verification script."
    update_result = _http_json(
        "PUT",
        _url(args.base_url, args.update_path, intent_code=args.intent_code),
        update_payload,
    )
    if update_result.status not in (200, 204):
        print(
            f"[FAIL] update intent status={update_result.status}, body={update_result.body}"
        )
        return 1
    print(f"[OK] update intent status={update_result.status}")

    delete_result = _http_json(
        "DELETE", _url(args.base_url, args.delete_path, intent_code=args.intent_code)
    )
    if delete_result.status not in (200, 202, 204, 404):
        print(
            f"[FAIL] delete intent status={delete_result.status}, body={delete_result.body}"
        )
        return 1
    print(f"[OK] delete intent status={delete_result.status}")
    print("[DONE] validation finished (full CRUD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

