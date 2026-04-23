#!/usr/bin/env python3
"""Verify the router assistant-facing non-stream contract without the assistant service."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    host_header: str | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if host_header:
        headers["Host"] = host_header
    request = urllib.request.Request(url=url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body_text}") from exc


def _create_session(
    *,
    base_url: str,
    timeout_seconds: float,
    host_header: str | None,
    session_id: str | None,
    cust_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if session_id:
        payload["session_id"] = session_id
    if cust_id:
        payload["cust_id"] = cust_id
    return _request_json(
        method="POST",
        url=f"{base_url}/api/router/v2/sessions",
        payload=payload,
        timeout_seconds=timeout_seconds,
        host_header=host_header,
    )


def _assistant_request_payload(
    *,
    txt: str,
    session_id: str,
    cust_id: str,
    current_display: str,
    agent_session_id: str,
    execution_mode: str,
) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "txt": txt,
        "custId": cust_id,
        "executionMode": execution_mode,
        "stream": False,
        "config_variables": [
            {"name": "custID", "value": cust_id},
            {"name": "sessionID", "value": session_id},
            {"name": "currentDisplay", "value": current_display},
            {"name": "agentSessionID", "value": agent_session_id},
        ],
    }


def _post_assistant_message(
    *,
    base_url: str,
    timeout_seconds: float,
    host_header: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return _request_json(
        method="POST",
        url=f"{base_url}/api/v1/message",
        payload=payload,
        timeout_seconds=timeout_seconds,
        host_header=host_header,
    )


def _output_kind(response: dict[str, Any]) -> str:
    nested_output = response.get("output")
    if isinstance(response.get("status"), str) and response.get("status") == "failed" and isinstance(response.get("errorCode"), str):
        return "failed"
    if isinstance(nested_output, dict) and nested_output.get("isHandOver") is True:
        return "handover"
    if isinstance(response.get("status"), str) and isinstance(response.get("intent_code"), str):
        return "router_state"
    return "unknown"


def _ensure_assistant_response_shape(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AssertionError(f"response must be a JSON object, got: {type(response).__name__}")
    if "snapshot" in response:
        raise AssertionError(f"assistant contract must not return snapshot: {response}")
    ok = response.get("ok")
    if not isinstance(ok, bool):
        raise AssertionError(f"assistant contract requires top-level boolean ok, got: {response}")
    output = response.get("output")
    if not isinstance(output, dict):
        raise AssertionError(f"assistant contract requires top-level object output, got: {response}")
    required_fields = [
        "current_task",
        "task_list",
        "completion_state",
        "completion_reason",
        "intent_code",
        "status",
        "message",
        "slot_memory",
        "output",
    ]
    missing = [field for field in required_fields if field not in response]
    if missing:
        raise AssertionError(f"assistant contract response missing fields {missing}: {response}")
    if not isinstance(response.get("task_list"), list):
        raise AssertionError(f"assistant contract task_list must be a list, got: {response}")
    if not isinstance(response.get("completion_state"), int):
        raise AssertionError(f"assistant contract completion_state must be an int, got: {response}")
    if not isinstance(response.get("completion_reason"), str):
        raise AssertionError(f"assistant contract completion_reason must be a string, got: {response}")
    if not isinstance(response.get("slot_memory"), dict):
        raise AssertionError(f"assistant contract slot_memory must be an object, got: {response}")

    kind = _output_kind(response)
    if kind == "unknown":
        raise AssertionError(f"assistant output shape is not recognized: {response}")

    if kind == "router_state":
        if not isinstance(response.get("message"), str):
            raise AssertionError(f"router_state output requires string message, got: {response}")
    elif kind == "handover":
        if not isinstance(response.get("intent_code"), str):
            raise AssertionError(f"handover output requires intent_code, got: {response}")
        if output.get("isHandOver") is not True:
            raise AssertionError(f"handover output requires isHandOver=true, got: {response}")
        if not isinstance(output.get("handOverReason"), str):
            raise AssertionError(f"handover output requires string handOverReason, got: {response}")
        if not isinstance(response.get("message"), str):
            raise AssertionError(f"handover output requires top-level string message, got: {response}")
        data = output.get("data")
        if data is not None and not isinstance(data, list):
            raise AssertionError(f"handover output data must be a list when present, got: {response}")
    elif kind == "failed":
        if not isinstance(response.get("message"), str):
            raise AssertionError(f"failed output requires string message, got: {response}")
        if output:
            raise AssertionError(f"failed output expects empty nested output, got: {response}")

    return {
        "ok": ok,
        "kind": kind,
        "status": response.get("status"),
        "intent_code": response.get("intent_code"),
        "response": response,
    }


def _strict_demo_check(
    *,
    turn_index: int,
    execution_mode: str,
    inspected: dict[str, Any],
) -> None:
    response = inspected["response"]
    kind = inspected["kind"]
    if turn_index == 1:
        if response["ok"] is not True:
            raise AssertionError(f"turn 1 expected ok=true for transfer demo, got: {response}")
        if kind != "router_state":
            raise AssertionError(f"turn 1 expected router_state for transfer demo, got: {response}")
        if response.get("status") != "waiting_user_input":
            raise AssertionError(f"turn 1 expected waiting_user_input, got: {response}")
        if response.get("completion_state") != 0:
            raise AssertionError(f"turn 1 expected completion_state=0, got: {response}")
    elif turn_index == 2 and execution_mode == "router_only":
        if response["ok"] is not True:
            raise AssertionError(f"turn 2 expected ok=true in router_only mode, got: {response}")
        if kind != "router_state":
            raise AssertionError(f"turn 2 expected router_state in router_only mode, got: {response}")
        if response.get("status") != "ready_for_dispatch":
            raise AssertionError(f"turn 2 expected ready_for_dispatch in router_only mode, got: {response}")
        if response.get("completion_state") != 0:
            raise AssertionError(f"turn 2 expected completion_state=0 in router_only mode, got: {response}")
    elif turn_index == 2 and execution_mode == "execute":
        if response["ok"] is not True:
            raise AssertionError(f"turn 2 expected ok=true in execute mode, got: {response}")
        if kind != "handover":
            raise AssertionError(f"turn 2 expected handover in execute mode, got: {response}")
        if response.get("completion_state") != 2:
            raise AssertionError(f"turn 2 expected completion_state=2 in execute mode, got: {response}")


def _print_json_block(title: str, payload: dict[str, Any]) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the router assistant-facing non-stream contract without the assistant service."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("INTENT_ROUTER_BASE_URL", "http://127.0.0.1:8000"),
        help="Router base URL.",
    )
    parser.add_argument("--host-header", default=os.getenv("INTENT_ROUTER_HOST_HEADER"), help="Optional Host header.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("INTENT_ROUTER_TIMEOUT_SECONDS", "60")),
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--execution-mode",
        choices=("router_only", "execute"),
        default="router_only",
        help="Router execution mode. router_only avoids downstream agent dependency.",
    )
    parser.add_argument(
        "--cust-id",
        default=os.getenv("INTENT_ROUTER_CUST_ID", "C0001"),
        help="Top-level cust_id plus config_variables.custID value.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional fixed session_id to create. Defaults to router-generated id.",
    )
    parser.add_argument(
        "--agent-session-id",
        default=None,
        help="Optional agentSessionID. Defaults to the created session_id.",
    )
    parser.add_argument(
        "--turn1",
        default="给小明转账",
        help="First turn text sent with assistant-style payload.",
    )
    parser.add_argument(
        "--turn2",
        default="200",
        help="Second turn text sent with assistant-style payload.",
    )
    parser.add_argument(
        "--display1",
        default="transfer_page",
        help="currentDisplay value for turn 1.",
    )
    parser.add_argument(
        "--display2",
        default="transfer_confirm_page",
        help="currentDisplay value for turn 2.",
    )
    parser.add_argument(
        "--single-turn",
        action="store_true",
        help="Only send the first turn and validate the returned shape.",
    )
    parser.add_argument(
        "--strict-demo",
        action="store_true",
        help="Enable strict transfer-demo assertions in addition to contract shape validation.",
    )
    parser.add_argument(
        "--output-mode",
        choices=("transcript", "report"),
        default="transcript",
        help="transcript prints raw request/response blocks; report prints one wrapped summary JSON.",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    session_create = _create_session(
        base_url=base_url,
        timeout_seconds=args.timeout_seconds,
        host_header=args.host_header,
        session_id=args.session_id,
        cust_id=args.cust_id,
    )
    session_id = str(session_create["session_id"])
    agent_session_id = args.agent_session_id or session_id

    if args.output_mode == "transcript":
        _print_json_block("session.create.response", session_create)

    turn_results: list[dict[str, Any]] = []
    turn_specs = [
        (1, args.turn1, args.display1),
    ]
    if not args.single_turn:
        turn_specs.append((2, args.turn2, args.display2))

    for turn_index, txt, display in turn_specs:
        request_payload = _assistant_request_payload(
            txt=txt,
            session_id=session_id,
            cust_id=args.cust_id,
            current_display=display,
            agent_session_id=agent_session_id,
            execution_mode=args.execution_mode,
        )
        response_payload = _post_assistant_message(
            base_url=base_url,
            timeout_seconds=args.timeout_seconds,
            host_header=args.host_header,
            payload=request_payload,
        )
        inspected = _ensure_assistant_response_shape(response_payload)
        if args.strict_demo:
            _strict_demo_check(
                turn_index=turn_index,
                execution_mode=args.execution_mode,
                inspected=inspected,
            )
        if args.output_mode == "transcript":
            _print_json_block(f"turn{turn_index}.request", request_payload)
            _print_json_block(f"turn{turn_index}.response", response_payload)
        turn_results.append(
            {
                "turn": turn_index,
                "request": request_payload,
                "response_kind": inspected["kind"],
                "response": response_payload,
            }
        )

    if args.output_mode == "report":
        print(
            json.dumps(
                {
                    "contract_ok": True,
                    "base_url": base_url,
                    "execution_mode": args.execution_mode,
                    "session": session_create,
                    "turns": turn_results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
