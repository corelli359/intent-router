#!/usr/bin/env python3
"""Simulate assistant-service requests sent directly to router-service.

This script bypasses assistant-service and posts the assistant-facing payload
straight into Router:

    POST /api/v1/message

Examples:

Non-stream:
    python scripts/simulate_assistant_request.py \
      --session-id demo_001 \
      --txt "给小明转账" \
      --current-display transfer_page \
      --no-stream

Stream:
    python scripts/simulate_assistant_request.py \
      --session-id demo_001 \
      --txt "200" \
      --current-display transfer_confirm_page \
      --stream
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Iterator


DEFAULT_BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_CUST_ID = "C0001"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_CURRENT_DISPLAY = "transfer_page"


def build_payload(
    *,
    session_id: str,
    txt: str,
    current_display: str,
    cust_id: str,
    execution_mode: str,
    agent_session_id: str | None,
    extra_slots: dict[str, Any] | None,
) -> dict[str, Any]:
    config_variables: list[dict[str, Any]] = [
        {"name": "custID", "value": cust_id},
        {"name": "sessionID", "value": session_id},
        {"name": "currentDisplay", "value": current_display},
        {"name": "agentSessionID", "value": agent_session_id or session_id},
    ]
    if extra_slots:
        config_variables.append(
            {
                "name": "slots_data",
                "value": json.dumps(extra_slots, ensure_ascii=False),
            }
        )
    return {
        "sessionId": session_id,
        "txt": txt,
        "custId": cust_id,
        "executionMode": execution_mode,
        "config_variables": config_variables,
    }


def _post_json(*, url: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def _iter_sse_frames(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
) -> Iterator[tuple[str, str]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    current_event: str | None = None
    data_lines: list[str] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if current_event is not None and data_lines:
                        yield current_event, "\n".join(data_lines)
                    current_event = None
                    data_lines = []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line.split(":", 1)[1].lstrip())
            if current_event is not None and data_lines:
                yield current_event, "\n".join(data_lines)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate assistant-to-router requests.")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Router base URL. Default: {DEFAULT_BASE_URL}.",
    )
    parser.add_argument("--session-id", default=f"assistant_router_sim_{int(time.time())}", help="Session id.")
    parser.add_argument("--txt", required=True, help="User input text.")
    parser.add_argument(
        "--current-display",
        default=DEFAULT_CURRENT_DISPLAY,
        help=f"currentDisplay config variable. Default: {DEFAULT_CURRENT_DISPLAY}.",
    )
    parser.add_argument("--cust-id", default=DEFAULT_CUST_ID, help="Customer id.")
    parser.add_argument(
        "--execution-mode",
        default="execute",
        choices=("execute", "router_only"),
        help="Assistant execution mode.",
    )
    parser.add_argument("--agent-session-id", default=None, help="Optional agentSessionID override.")
    parser.add_argument(
        "--slots-data",
        default=None,
        help="Optional JSON string forwarded as slots_data config variable.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Request timeout in seconds.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--stream", dest="stream", action="store_true", help="Use Router SSE mode.")
    mode_group.add_argument("--no-stream", dest="stream", action="store_false", help="Use Router JSON mode.")
    parser.set_defaults(stream=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    extra_slots = json.loads(args.slots_data) if args.slots_data else None
    payload = build_payload(
        session_id=args.session_id,
        txt=args.txt,
        current_display=args.current_display,
        cust_id=args.cust_id,
        execution_mode=args.execution_mode,
        agent_session_id=args.agent_session_id,
        extra_slots=extra_slots,
    )

    print("=== request ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()

    if args.stream:
        payload["stream"] = True
        url = f"{args.base_url.rstrip('/')}/api/v1/message"
        print(f"POST {url}")
        start = time.time()
        for index, (event, data) in enumerate(
            _iter_sse_frames(url=url, payload=payload, timeout_seconds=args.timeout),
            start=1,
        ):
            elapsed = time.time() - start
            print(f"--- frame {index} @ +{elapsed:.3f}s ---")
            print(f"event: {event}")
            if data == "[DONE]":
                print("data: [DONE]")
                continue
            try:
                print(json.dumps(json.loads(data), ensure_ascii=False, indent=2))
            except Exception:
                print(data)
        return 0

    payload["stream"] = False
    url = f"{args.base_url.rstrip('/')}/api/v1/message"
    print(f"POST {url}")
    response = _post_json(url=url, payload=payload, timeout_seconds=args.timeout)
    print("=== response ===")
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
