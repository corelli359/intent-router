#!/usr/bin/env python3
"""Mock assistant-service and call router directly over the assistant protocol.

This script does not call assistant-service. It builds the same assistant ->
router payload and posts it straight to:

    POST /api/v1/message

Default behavior is streaming (`stream=true`).

CLI example:

    python scripts/mock_assistant_router_stream.py \
      --session-id demo_transfer_001 \
      --txt "给小明转账" \
      --current-display transfer_page

Jupyter example:

    from scripts.mock_assistant_router_stream import run_one_turn

    run_one_turn(
        session_id="demo_transfer_001",
        txt="给小明转账",
        current_display="transfer_page",
    )
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


DEFAULT_BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://intent-router.kkrrc-359.top")
DEFAULT_CUST_ID = "C0001"
DEFAULT_CURRENT_DISPLAY = "transfer_page"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_SESSION_ID_PREFIX = "mock_assistant"


@dataclass(slots=True)
class SSEFrame:
    event: str
    data: str

    def json_data(self) -> Any:
        if self.data == "[DONE]":
            return self.data
        return json.loads(self.data)


def default_session_id() -> str:
    """Return a notebook/CLI friendly default assistant session id."""
    return f"{DEFAULT_SESSION_ID_PREFIX}_{int(time.time())}"


def build_assistant_to_router_payload(
    *,
    session_id: str,
    txt: str,
    current_display: str = DEFAULT_CURRENT_DISPLAY,
    cust_id: str = DEFAULT_CUST_ID,
    execution_mode: str = "execute",
    agent_session_id: str | None = None,
    stream: bool = True,
    slots_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the same payload shape assistant-service forwards to router."""
    config_variables: list[dict[str, Any]] = [
        {"name": "custID", "value": cust_id},
        {"name": "sessionID", "value": session_id},
        {"name": "currentDisplay", "value": current_display},
        {"name": "agentSessionID", "value": agent_session_id or session_id},
    ]
    if slots_data:
        config_variables.append(
            {
                "name": "slots_data",
                "value": json.dumps(slots_data, ensure_ascii=False),
            }
        )

    return {
        "sessionId": session_id,
        "txt": txt,
        "config_variables": config_variables,
        "executionMode": execution_mode,
        "custId": cust_id,
        "stream": stream,
    }


def router_message_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/message"


def _request_json(*, url: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
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
) -> Iterator[SSEFrame]:
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
                        yield SSEFrame(event=current_event, data="\n".join(data_lines))
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
                yield SSEFrame(event=current_event, data="\n".join(data_lines))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def print_frames(frames: list[SSEFrame]) -> None:
    for index, frame in enumerate(frames, start=1):
        print(f"--- frame {index} ---")
        print(f"event: {frame.event}")
        if frame.data == "[DONE]":
            print("data: [DONE]")
            continue
        try:
            print(json.dumps(frame.json_data(), ensure_ascii=False, indent=2))
        except Exception:
            print(frame.data)


def run_one_turn(
    *,
    session_id: str | None = None,
    txt: str,
    current_display: str = DEFAULT_CURRENT_DISPLAY,
    base_url: str = DEFAULT_BASE_URL,
    cust_id: str = DEFAULT_CUST_ID,
    execution_mode: str = "execute",
    agent_session_id: str | None = None,
    stream: bool = True,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    slots_data: dict[str, Any] | None = None,
    print_request: bool = True,
    print_response: bool = True,
) -> list[SSEFrame] | dict[str, Any]:
    """Run one assistant-like turn directly against router."""
    resolved_session_id = session_id or default_session_id()
    payload = build_assistant_to_router_payload(
        session_id=resolved_session_id,
        txt=txt,
        current_display=current_display,
        cust_id=cust_id,
        execution_mode=execution_mode,
        agent_session_id=agent_session_id,
        stream=stream,
        slots_data=slots_data,
    )
    url = router_message_url(base_url)

    if print_request:
        print("=== request ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"POST {url}")
        print()

    if not stream:
        response = _request_json(url=url, payload=payload, timeout_seconds=timeout_seconds)
        if print_response:
            print("=== response ===")
            print(json.dumps(response, ensure_ascii=False, indent=2))
        return response

    frames: list[SSEFrame] = []
    start = time.time()
    for index, frame in enumerate(
        _iter_sse_frames(url=url, payload=payload, timeout_seconds=timeout_seconds),
        start=1,
    ):
        frames.append(frame)
        if not print_response:
            continue
        elapsed = time.time() - start
        print(f"--- frame {index} @ +{elapsed:.3f}s ---")
        print(f"event: {frame.event}")
        if frame.data == "[DONE]":
            print("data: [DONE]")
            print()
            continue
        try:
            print(json.dumps(frame.json_data(), ensure_ascii=False, indent=2))
        except Exception:
            print(frame.data)
        print()
    return frames


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock assistant-service and stream directly to router.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Router base URL. Default: {DEFAULT_BASE_URL}")
    parser.add_argument("--session-id", default=default_session_id(), help="Assistant session id.")
    parser.add_argument("--txt", required=True, help="User text.")
    parser.add_argument(
        "--current-display",
        default=DEFAULT_CURRENT_DISPLAY,
        help=f"Assistant currentDisplay. Default: {DEFAULT_CURRENT_DISPLAY}",
    )
    parser.add_argument("--cust-id", default=DEFAULT_CUST_ID, help=f"custId. Default: {DEFAULT_CUST_ID}")
    parser.add_argument(
        "--execution-mode",
        default="execute",
        choices=("execute", "router_only"),
        help="Assistant executionMode.",
    )
    parser.add_argument("--agent-session-id", default=None, help="Optional agentSessionID override.")
    parser.add_argument("--slots-data", default=None, help="Optional JSON string passed as slots_data.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout in seconds.")
    parser.add_argument("--no-stream", dest="stream", action="store_false", help="Use non-stream JSON mode.")
    parser.set_defaults(stream=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    slots_data = json.loads(args.slots_data) if args.slots_data else None
    run_one_turn(
        session_id=args.session_id,
        txt=args.txt,
        current_display=args.current_display,
        base_url=args.base_url,
        cust_id=args.cust_id,
        execution_mode=args.execution_mode,
        agent_session_id=args.agent_session_id,
        stream=args.stream,
        timeout_seconds=args.timeout,
        slots_data=slots_data,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
