from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app import TransferAgentRuntime, TransferAgentTurnRequest  # noqa: E402


class FakeTransferAgentRuntime(TransferAgentRuntime):
    def __init__(self, task_payload: dict[str, Any]) -> None:
        super().__init__()
        self.task_payload = task_payload
        self.agent_outputs: list[dict[str, Any]] = []

    async def _load_router_task(  # type: ignore[override]
        self,
        request: TransferAgentTurnRequest,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        events.append({"type": "agent.router_task_loaded", "output": {"task": self.task_payload}})
        return {"found": True, "session_id": request.session_id, "task": self.task_payload}

    async def _post_agent_output(  # type: ignore[override]
        self,
        *,
        request: TransferAgentTurnRequest,
        status: str,
        output: dict[str, Any],
        events: list[dict[str, Any]],
        ishandover: bool | None = None,
    ) -> dict[str, Any]:
        payload = {
            "session_id": request.session_id,
            "task_id": request.task_id,
            "status": status,
            "output": output,
        }
        if ishandover is not None:
            payload["ishandover"] = ishandover
        self.agent_outputs.append(payload)
        events.append({"type": "agent.router_callback", "input": payload})
        return {"status": "task_updated", "agent_output": output}


def _request(message: str) -> TransferAgentTurnRequest:
    return TransferAgentTurnRequest(session_id="sess-test", task_id="task-test", message=message)


def _run(runtime: TransferAgentRuntime, message: str) -> dict[str, Any]:
    return asyncio.run(runtime.handle_turn(_request(message)))


def test_transfer_agent_extracts_slots_from_free_utterance_before_confirmation() -> None:
    runtime = FakeTransferAgentRuntime(
        {
            "task_id": "task-test",
            "scene_id": "transfer",
            "target_agent": "transfer-agent",
            "routing_hints": {},
        }
    )

    first = _run(runtime, "我要转账300给小红")

    assert first["assistant_message"] == "请确认：向小红转账300元。确认办理吗？"
    assert first["agent_state"]["recipient"] == "小红"
    assert first["agent_state"]["amount"] == "300"
    assert first["agent_state"]["skill_step"] == "waiting_confirmation"
    assert runtime.agent_outputs == []
    fourth = _run(runtime, "确认")
    assert fourth["status"] == "completed"
    assert fourth["router_update"]["agent_output"]["data"][0]["status"] == "success"


def test_transfer_agent_asks_all_missing_fields_then_accepts_free_reply() -> None:
    runtime = FakeTransferAgentRuntime(
        {
            "task_id": "task-test",
            "scene_id": "transfer",
            "target_agent": "transfer-agent",
            "routing_hints": {},
        }
    )

    first = _run(runtime, "我要转账")
    second = _run(runtime, "小红300")

    assert first["assistant_message"] == "可以，请告诉我转给谁、转账金额是多少？"
    assert first["agent_state"]["recipient"] is None
    assert first["agent_state"]["amount"] is None
    assert second["assistant_message"] == "请确认：向小红转账300元。确认办理吗？"
    assert second["agent_state"]["recipient"] == "小红"
    assert second["agent_state"]["amount"] == "300"
    assert runtime.agent_outputs == []


def test_transfer_agent_handover_uses_router_contract_for_wrong_task() -> None:
    runtime = FakeTransferAgentRuntime(
        {
            "task_id": "task-test",
            "scene_id": "fund_query",
            "target_agent": "fund-agent",
            "routing_hints": {},
        }
    )

    output = _run(runtime, "我要转账")

    assert output["status"] == "handover"
    assert runtime.agent_outputs[0]["ishandover"] is True
    assert runtime.agent_outputs[0]["output"]["data"] == []
