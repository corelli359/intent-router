from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app import AssistantRuntime, AssistantTurnRequest  # noqa: E402


class FakeAssistantRuntime(AssistantRuntime):
    def __init__(self, router_outputs: list[dict[str, Any]], agent_outputs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.router_outputs = router_outputs
        self.agent_outputs = agent_outputs
        self.router_calls: list[str] = []
        self.agent_calls: list[str] = []

    async def _call_router(  # type: ignore[override]
        self,
        *,
        request: AssistantTurnRequest,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.router_calls.append(request.message)
        events.append({"type": "assistant.router_request", "service": "assistant-service"})
        return self.router_outputs.pop(0)

    async def _call_transfer_agent(  # type: ignore[override]
        self,
        *,
        request: AssistantTurnRequest,
        task_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.agent_calls.append(f"{task_id}:{request.message}")
        events.append({"type": "assistant.agent_request", "service": "assistant-service"})
        return self.agent_outputs.pop(0)


def _request(message: str) -> AssistantTurnRequest:
    return AssistantTurnRequest(session_id="sess-a", message=message, user_profile={"user_id": "U1"})


def _run(runtime: AssistantRuntime, message: str) -> dict[str, Any]:
    return asyncio.run(runtime.handle_turn(_request(message)))


def test_assistant_is_front_door_and_generates_transfer_result_text() -> None:
    runtime = FakeAssistantRuntime(
        router_outputs=[
            {
                "status": "dispatched",
                "scene_id": "transfer",
                "target_agent": "transfer-agent",
                "task_id": "task-a",
                "events": [{"type": "scene_selected", "scene_id": "transfer"}],
            }
        ],
        agent_outputs=[
            {
                "status": "completed",
                "assistant_message": "agent internal text",
                "router_update": {
                    "status": "task_updated",
                    "task_id": "task-a",
                    "agent_output": {
                        "data": [
                            {
                                "type": "transfer_result",
                                "status": "success",
                                "recipient": "小红",
                                "amount": "300",
                            }
                        ]
                    },
                    "events": [{"type": "task.completed"}],
                },
                "events": [{"type": "agent.skill_loaded", "service": "transfer-agent"}],
            }
        ],
    )

    output = _run(runtime, "我要转账300给小红")

    assert runtime.router_calls == ["我要转账300给小红"]
    assert runtime.agent_calls == ["task-a:我要转账300给小红"]
    assert output["assistant_message"] == "转账成功，已向小红转账300元。"
    assert output["output"]["status"] == "task_updated"
    assert output["assistant_state"]["active_task_id"] is None


def test_assistant_calls_router_only_for_non_transfer_scene() -> None:
    runtime = FakeAssistantRuntime(
        router_outputs=[
            {
                "status": "dispatched",
                "scene_id": "fund_query",
                "target_agent": "fund-agent",
                "task_id": "task-f",
                "events": [{"type": "scene_selected", "scene_id": "fund_query"}],
            }
        ],
        agent_outputs=[],
    )

    output = _run(runtime, "查一下基金")

    assert runtime.router_calls == ["查一下基金"]
    assert runtime.agent_calls == []
    assert output["router_output"]["target_agent"] == "fund-agent"
