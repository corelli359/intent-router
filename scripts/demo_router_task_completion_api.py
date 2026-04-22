#!/usr/bin/env python3
"""Deterministic assistant callback demo for Router task completion.

Direct run, no extra parameters required:

    python scripts/demo_router_task_completion_api.py

Default mode:
1. Build an in-process Router app
2. Use a mock downstream agent that returns `completion_state=1`
3. Automatically run:
   - `/api/v1/message` turn 1: 给小明转账
   - `/api/v1/message` turn 2: 200
   - `/api/v1/task/completion` with assistant signal 1
   - `/api/v1/task/completion` with assistant signal 2
4. Print the full request/response chain

Optional remote mode:

    INTENT_ROUTER_BASE_URL=http://your-router-host python scripts/demo_router_task_completion_api.py

In remote mode, the script still chains Router interfaces automatically, but the
callback path can only be exercised when the deployed downstream agent actually
returns `completion_state=1`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
ROUTER_SRC = ROOT / "backend" / "services" / "router-service" / "src"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(ROUTER_SRC) not in sys.path:
    sys.path.insert(0, str(ROUTER_SRC))

from tests.test_router_api_v2 import (  # noqa: E402
    _AssistantProtocolPartialCompletionAgentClient,
    _ContractTransferUnderstandingValidator,
    _TransferOnlyRecognizer,
    _assistant_protocol_ag_trans_intent,
    _test_v2_app,
)


BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "").strip()
TURN_1_TEXT = "给小明转账"
TURN_2_TEXT = "200"
DISPLAY_1 = "transfer_page"
DISPLAY_2 = "transfer_confirm_page"
EXECUTION_MODE = "execute"


def print_block(title: str, payload: dict[str, Any]) -> None:
    print(f"=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def assistant_message_payload(*, session_id: str, txt: str, current_display: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "txt": txt,
        "executionMode": EXECUTION_MODE,
        "stream": False,
        "config_variables": [
            {"name": "sessionID", "value": session_id},
            {"name": "currentDisplay", "value": current_display},
            {"name": "agentSessionID", "value": session_id},
        ],
    }


def completion_payload(*, session_id: str, task_id: str, completion_signal: int) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "taskId": task_id,
        "completionSignal": completion_signal,
    }


def assert_output_shape(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise AssertionError(f"response must be a JSON object: {response!r}")
    if not isinstance(response.get("ok"), bool):
        raise AssertionError(f"response requires top-level boolean ok: {response}")
    output = response.get("output")
    if not isinstance(output, dict):
        raise AssertionError(f"response requires top-level object output: {response}")
    required_fields = [
        "current_task",
        "task_list",
        "completion_state",
        "completion_reason",
        "node_id",
        "intent_code",
        "status",
        "isHandOver",
        "handOverReason",
        "message",
        "data",
        "slot_memory",
    ]
    missing = [field for field in required_fields if field not in output]
    if missing:
        raise AssertionError(f"response output missing fields {missing}: {response}")
    return output


def _request_json(method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to call {url}: {exc}") from exc


async def _run_local_scenario(*, session_id: str, completion_signal: int) -> None:
    app, _ = _test_v2_app(
        recognizer=_TransferOnlyRecognizer(),
        intents=[_assistant_protocol_ag_trans_intent()],
        understanding_validator=_ContractTransferUnderstandingValidator(),
        agent_client=_AssistantProtocolPartialCompletionAgentClient(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://router.local",
    ) as client:
        turn1_request = assistant_message_payload(
            session_id=session_id,
            txt=TURN_1_TEXT,
            current_display=DISPLAY_1,
        )
        turn1_response = (await client.post("/api/v1/message", json=turn1_request)).json()
        turn1_output = assert_output_shape(turn1_response)

        turn2_request = assistant_message_payload(
            session_id=session_id,
            txt=TURN_2_TEXT,
            current_display=DISPLAY_2,
        )
        turn2_response = (await client.post("/api/v1/message", json=turn2_request)).json()
        turn2_output = assert_output_shape(turn2_response)

        callback_request = completion_payload(
            session_id=session_id,
            task_id=turn2_output["current_task"],
            completion_signal=completion_signal,
        )
        callback_response = (await client.post("/api/v1/task/completion", json=callback_request)).json()
        callback_output = assert_output_shape(callback_response)

    print_block(f"scenario[{completion_signal}].turn1.request", turn1_request)
    print_block(f"scenario[{completion_signal}].turn1.response", turn1_response)
    print_block(f"scenario[{completion_signal}].turn2.request", turn2_request)
    print_block(f"scenario[{completion_signal}].turn2.response", turn2_response)
    print_block(f"scenario[{completion_signal}].callback.request", callback_request)
    print_block(f"scenario[{completion_signal}].callback.response", callback_response)

    if turn2_output["completion_state"] != 1:
        raise AssertionError(f"expected turn2 completion_state=1, got: {turn2_response}")
    if turn2_output["status"] != "waiting_assistant_completion":
        raise AssertionError(f"expected waiting_assistant_completion before callback, got: {turn2_response}")
    if completion_signal == 1 and callback_output["completion_reason"] != "joint_done":
        raise AssertionError(f"expected joint_done for assistant signal 1, got: {callback_response}")
    if completion_signal == 2 and callback_output["completion_reason"] != "assistant_final_done":
        raise AssertionError(f"expected assistant_final_done for assistant signal 2, got: {callback_response}")


def _run_remote_scenario(*, base_url: str) -> None:
    session_id = f"assistant_remote_completion_{int(time.time())}"
    print("=== remote mode ===")
    print(json.dumps(
        {
            "base_url": base_url,
            "session_id": session_id,
            "turn_1": TURN_1_TEXT,
            "turn_2": TURN_2_TEXT,
        },
        ensure_ascii=False,
        indent=2,
    ))
    print()

    turn1_request = assistant_message_payload(
        session_id=session_id,
        txt=TURN_1_TEXT,
        current_display=DISPLAY_1,
    )
    turn1_response = _request_json("POST", f"{base_url.rstrip('/')}/api/v1/message", turn1_request)
    turn1_output = assert_output_shape(turn1_response)
    print_block("remote.turn1.request", turn1_request)
    print_block("remote.turn1.response", turn1_response)

    turn2_request = assistant_message_payload(
        session_id=session_id,
        txt=TURN_2_TEXT,
        current_display=DISPLAY_2,
    )
    turn2_response = _request_json("POST", f"{base_url.rstrip('/')}/api/v1/message", turn2_request)
    turn2_output = assert_output_shape(turn2_response)
    print_block("remote.turn2.request", turn2_request)
    print_block("remote.turn2.response", turn2_response)

    if not (
        isinstance(turn2_output.get("current_task"), str)
        and turn2_output["current_task"].startswith("task_")
        and turn2_output.get("completion_state") == 1
    ):
        print("=== remote.callback.status ===")
        print("当前远端链路没有进入 agent_partial_done，所以这次不会自动继续调 /api/v1/task/completion。")
        print("如果要在远端演示 callback，部署的下游 agent 必须先返回 completion_state=1。")
        print()
        return

    for completion_signal in (1, 2):
        callback_request = completion_payload(
            session_id=session_id,
            task_id=turn2_output["current_task"],
            completion_signal=completion_signal,
        )
        callback_response = _request_json(
            "POST",
            f"{base_url.rstrip('/')}/api/v1/task/completion",
            callback_request,
        )
        print_block(f"remote.callback[{completion_signal}].request", callback_request)
        print_block(f"remote.callback[{completion_signal}].response", callback_response)


def main() -> int:
    if BASE_URL:
        _run_remote_scenario(base_url=BASE_URL)
        return 0

    asyncio.run(_run_local_scenario(session_id="assistant_local_callback_1", completion_signal=1))
    asyncio.run(_run_local_scenario(session_id="assistant_local_callback_2", completion_signal=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
