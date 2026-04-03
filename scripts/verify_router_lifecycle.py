#!/usr/bin/env python3
"""Verify router task lifecycle with SSE and multi-turn message resume."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class SseEvent:
    event: str
    data: dict[str, Any]


def _http_json(method: str, url: str, payload: dict[str, Any], timeout: float = 8.0) -> int:
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _build_url(base_url: str, path: str, session_id: str) -> str:
    sid = urllib.parse.quote(session_id, safe="")
    return f"{base_url.rstrip('/')}/{path.format(session_id=sid).lstrip('/')}"


def _sse_reader(url: str, output: "queue.Queue[SseEvent]", stop: threading.Event) -> None:
    req = urllib.request.Request(
        url=url,
        method="GET",
        headers={"Accept": "text/event-stream"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            event_name = ""
            data_lines: list[str] = []
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if text == "":
                    if event_name:
                        payload: dict[str, Any] = {}
                        if data_lines:
                            joined = "\n".join(data_lines)
                            try:
                                payload = json.loads(joined)
                            except json.JSONDecodeError:
                                payload = {"raw": joined}
                        output.put(SseEvent(event=event_name, data=payload))
                    event_name = ""
                    data_lines = []
                    continue
                if text.startswith("event:"):
                    event_name = text.split(":", 1)[1].strip()
                elif text.startswith("data:"):
                    data_lines.append(text.split(":", 1)[1].strip())
    except Exception as exc:  # noqa: BLE001
        output.put(SseEvent(event="__error__", data={"error": str(exc)}))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify router lifecycle via SSE.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--session-id", default="mvp-session-001")
    parser.add_argument("--events-path", default="/api/router/sessions/{session_id}/events")
    parser.add_argument(
        "--message-path", default="/api/router/sessions/{session_id}/messages"
    )
    parser.add_argument("--initial-message", default="check order and cancel appointment")
    parser.add_argument("--followup-message", default="order id is 123")
    parser.add_argument(
        "--expected-events",
        default="task.waiting_user_input,task.completed",
        help="Comma separated ordered events to wait for.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=40.0)
    return parser.parse_args()


def _await_sequence(
    events_queue: "queue.Queue[SseEvent]",
    expected: list[str],
    timeout_seconds: float,
) -> tuple[bool, list[SseEvent]]:
    deadline = time.time() + timeout_seconds
    received: list[SseEvent] = []
    idx = 0

    while time.time() < deadline and idx < len(expected):
        remaining = max(0.1, deadline - time.time())
        try:
            evt = events_queue.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        received.append(evt)
        if evt.event == "__error__":
            return False, received
        if evt.event == expected[idx]:
            idx += 1
    return idx == len(expected), received


def main() -> int:
    args = parse_args()
    expected = [item.strip() for item in args.expected_events.split(",") if item.strip()]
    if not expected:
        print("[FAIL] expected-events cannot be empty")
        return 1

    events_url = _build_url(args.base_url, args.events_path, args.session_id)
    messages_url = _build_url(args.base_url, args.message_path, args.session_id)

    events_queue: "queue.Queue[SseEvent]" = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(
        target=_sse_reader,
        args=(events_url, events_queue, stop),
        daemon=True,
    )
    t.start()
    time.sleep(0.4)

    try:
        status = _http_json(
            "POST",
            messages_url,
            {"message": args.initial_message, "session_id": args.session_id},
        )
    except urllib.error.HTTPError as exc:
        print(f"[FAIL] initial message status={exc.code}")
        stop.set()
        t.join(timeout=1.0)
        return 1
    print(f"[OK] initial message accepted status={status}")

    saw_waiting = False
    received: list[SseEvent] = []
    deadline = time.time() + args.timeout_seconds
    expected_idx = 0

    while time.time() < deadline and expected_idx < len(expected):
        try:
            evt = events_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        received.append(evt)
        if evt.event == "__error__":
            print(f"[FAIL] SSE reader error: {evt.data}")
            stop.set()
            t.join(timeout=1.0)
            return 1

        print(f"[SSE] event={evt.event} data={evt.data}")

        if evt.event == "task.waiting_user_input" and not saw_waiting:
            saw_waiting = True
            try:
                follow_status = _http_json(
                    "POST",
                    messages_url,
                    {"message": args.followup_message, "session_id": args.session_id},
                )
                print(f"[OK] follow-up message accepted status={follow_status}")
            except urllib.error.HTTPError as exc:
                print(f"[FAIL] follow-up message status={exc.code}")
                stop.set()
                t.join(timeout=1.0)
                return 1

        if evt.event == expected[expected_idx]:
            expected_idx += 1

    stop.set()
    t.join(timeout=1.0)

    if expected_idx != len(expected):
        seen = [evt.event for evt in received]
        print(f"[FAIL] expected sequence {expected} not completed; seen={seen}")
        return 1

    print(f"[DONE] expected event sequence observed: {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

