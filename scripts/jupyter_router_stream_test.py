#!/usr/bin/env python3
"""Notebook-friendly direct router SSE test client.

Use cases:
1. Run directly:

    python scripts/jupyter_router_stream_test.py

2. In Jupyter:

    %run scripts/jupyter_router_stream_test.py

   or:

    from scripts.jupyter_router_stream_test import run_transfer_two_turn_demo
    run_transfer_two_turn_demo()

This script talks to router-service directly and does not call assistant-service.

This script only uses the production router SSE entry:

    POST /api/v1/message

The returned SSE frames follow the assistant-facing contract:

    event: message
    data: {...assistant output...}
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# Change only these constants before running on VPS / K8s.
# ---------------------------------------------------------------------------

ROUTER_BASE_URL = "http://127.0.0.1:8000"
CUST_ID = "C0001"
SESSION_ID = f"router_stream_{int(time.time())}"
EXECUTION_MODE = "execute"  # "execute" or "router_only"
TIMEOUT_SECONDS = 300
# Turn 1 / Turn 2 demo for "single intent + slot fill + agent"
TURN_1_TEXT = "给小明转账"
TURN_1_CURRENT_DISPLAY = "transfer_page"

TURN_2_TEXT = "200"
TURN_2_CURRENT_DISPLAY = "transfer_confirm_page"


@dataclass
class SSEFrame:
    event: str
    data: str

    def json_data(self) -> Any:
        if self.data == "[DONE]":
            return self.data
        return json.loads(self.data)


def create_session_url(base_url: str = ROUTER_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/api/router/v2/sessions"


def router_stream_url(
    session_id: str,
    base_url: str = ROUTER_BASE_URL,
) -> str:
    del session_id
    return f"{base_url.rstrip('/')}/api/v1/message"


def router_snapshot_url(session_id: str, base_url: str = ROUTER_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/api/router/v2/sessions/{session_id}"


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: int = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


def create_session(
    *,
    base_url: str = ROUTER_BASE_URL,
    cust_id: str = CUST_ID,
    session_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"cust_id": cust_id}
    if session_id:
        payload["session_id"] = session_id
    return _request_json(method="POST", url=create_session_url(base_url), payload=payload)


def fetch_snapshot(
    *,
    session_id: str,
    base_url: str = ROUTER_BASE_URL,
) -> dict[str, Any]:
    return _request_json(method="GET", url=router_snapshot_url(session_id, base_url), payload=None)


def build_assistant_payload(
    *,
    session_id: str,
    txt: str,
    current_display: str,
    cust_id: str = CUST_ID,
    execution_mode: str = EXECUTION_MODE,
    agent_session_id: str | None = None,
    slots_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "custId": cust_id,
        "executionMode": execution_mode,
        "stream": True,
        "config_variables": config_variables,
    }


def build_payload(
    *,
    session_id: str,
    txt: str,
    current_display: str = "",
    cust_id: str = CUST_ID,
    execution_mode: str = EXECUTION_MODE,
    agent_session_id: str | None = None,
    slots_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_assistant_payload(
        session_id=session_id,
        txt=txt,
        current_display=current_display,
        cust_id=cust_id,
        execution_mode=execution_mode,
        agent_session_id=agent_session_id,
        slots_data=slots_data,
    )


def _post_stream(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int = TIMEOUT_SECONDS,
) -> list[SSEFrame]:
    return list(_iter_sse_frames(url=url, payload=payload, timeout_seconds=timeout_seconds))


def _iter_sse_frames(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int = TIMEOUT_SECONDS,
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


def print_live_frames(
    *,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int = TIMEOUT_SECONDS,
) -> list[SSEFrame]:
    frames: list[SSEFrame] = []
    start_time = time.time()
    for index, frame in enumerate(
        _iter_sse_frames(url=url, payload=payload, timeout_seconds=timeout_seconds),
        start=1,
    ):
        frames.append(frame)
        elapsed = time.time() - start_time
        print(f"--- frame {index} @ +{elapsed:.3f}s ---", flush=True)
        print(f"event: {frame.event}", flush=True)
        if frame.data == "[DONE]":
            print("data: [DONE]", flush=True)
            continue
        try:
            print(json.dumps(frame.json_data(), ensure_ascii=False, indent=2), flush=True)
        except Exception:
            print(frame.data, flush=True)
    print(flush=True)
    return frames


def run_one_turn(
    *,
    session_id: str,
    txt: str,
    current_display: str = "",
    base_url: str = ROUTER_BASE_URL,
    cust_id: str = CUST_ID,
    execution_mode: str = EXECUTION_MODE,
    agent_session_id: str | None = None,
    slots_data: dict[str, Any] | None = None,
) -> list[SSEFrame]:
    payload = build_payload(
        session_id=session_id,
        txt=txt,
        current_display=current_display,
        cust_id=cust_id,
        execution_mode=execution_mode,
        agent_session_id=agent_session_id,
        slots_data=slots_data,
    )
    print("=== request ===")
    print(f"POST {router_stream_url(session_id, base_url)}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()
    frames = _post_stream(url=router_stream_url(session_id, base_url), payload=payload)
    print("=== response frames ===")
    print_frames(frames)
    print()
    return frames


def run_one_turn_live(
    *,
    session_id: str,
    txt: str,
    current_display: str = "",
    base_url: str = ROUTER_BASE_URL,
    cust_id: str = CUST_ID,
    execution_mode: str = EXECUTION_MODE,
    agent_session_id: str | None = None,
    slots_data: dict[str, Any] | None = None,
) -> list[SSEFrame]:
    payload = build_payload(
        session_id=session_id,
        txt=txt,
        current_display=current_display,
        cust_id=cust_id,
        execution_mode=execution_mode,
        agent_session_id=agent_session_id,
        slots_data=slots_data,
    )
    print("=== request ===")
    print(f"POST {router_stream_url(session_id, base_url)}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()
    print("=== live response frames ===", flush=True)
    return print_live_frames(url=router_stream_url(session_id, base_url), payload=payload)


def run_transfer_two_turn_demo(
    *,
    base_url: str = ROUTER_BASE_URL,
    session_id: str = SESSION_ID,
    cust_id: str = CUST_ID,
    execution_mode: str = EXECUTION_MODE,
    create_new_session: bool = True,
) -> dict[str, Any]:
    """Run the standard two-turn transfer scenario directly against router SSE."""
    if create_new_session:
        create_response = create_session(
            base_url=base_url,
            cust_id=cust_id,
            session_id=session_id,
        )
        print("=== session.create.response ===")
        print(json.dumps(create_response, ensure_ascii=False, indent=2))
        print()
        session_id = create_response["session_id"]

    print(f"Using session_id: {session_id}")
    print()

    turn_1_frames = run_one_turn(
        session_id=session_id,
        txt=TURN_1_TEXT,
        current_display=TURN_1_CURRENT_DISPLAY,
        base_url=base_url,
        cust_id=cust_id,
        execution_mode=execution_mode,
        agent_session_id=session_id,
    )
    turn_2_frames = run_one_turn(
        session_id=session_id,
        txt=TURN_2_TEXT,
        current_display=TURN_2_CURRENT_DISPLAY,
        base_url=base_url,
        cust_id=cust_id,
        execution_mode=execution_mode,
        agent_session_id=session_id,
    )
    return {
        "session_id": session_id,
        "turn_1": turn_1_frames,
        "turn_2": turn_2_frames,
    }


def main() -> int:
    run_transfer_two_turn_demo()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
