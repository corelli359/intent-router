from __future__ import annotations

from typing import Any
import uuid

import httpx

from router_v4_service.core.models import AgentDefinition, AgentDispatchResult


class AgentDispatchError(RuntimeError):
    """Raised when Router cannot dispatch to an execution agent."""


class AgentDispatchClient:
    """Dispatches router tasks to execution agents.

    `mock://` endpoints are provided for local tests and demos. Real deployments
    should register `http://` or `https://` endpoints.
    """

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    def dispatch(
        self,
        *,
        agent: AgentDefinition,
        task_payload: dict[str, Any],
    ) -> AgentDispatchResult:
        if not any(scene_id == task_payload.get("scene_id") for scene_id in agent.accepted_scene_ids):
            raise AgentDispatchError(
                f"agent {agent.agent_id} does not accept scene {task_payload.get('scene_id')}"
            )
        if agent.endpoint.startswith("mock://"):
            return self._mock_dispatch(agent=agent, task_payload=task_payload)
        if agent.endpoint.startswith("http://") or agent.endpoint.startswith("https://"):
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(agent.endpoint, json=task_payload)
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict):
                raise AgentDispatchError(f"agent {agent.agent_id} returned a non-object payload")
            return AgentDispatchResult(
                agent_task_id=str(payload.get("agent_task_id") or payload.get("task_id") or ""),
                status=str(payload.get("status") or "dispatched"),
                message=str(payload.get("message") or "已派发给执行 Agent。"),
                raw=payload,
            )
        raise AgentDispatchError(f"unsupported agent endpoint: {agent.endpoint}")

    def forward_message(
        self,
        *,
        agent_id: str,
        agent_task_id: str,
        message: str,
    ) -> AgentDispatchResult:
        return AgentDispatchResult(
            agent_task_id=agent_task_id,
            status="forwarded",
            message=f"已将消息转交给 {agent_id} 继续处理。",
            raw={"message": message, "agent_id": agent_id},
        )

    def _mock_dispatch(
        self,
        *,
        agent: AgentDefinition,
        task_payload: dict[str, Any],
    ) -> AgentDispatchResult:
        seed = f"{agent.agent_id}:{task_payload.get('router_session_id')}:{task_payload.get('raw_message')}"
        task_id = "task_" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:12]
        scene_id = str(task_payload.get("scene_id") or "")
        return AgentDispatchResult(
            agent_task_id=task_id,
            status="dispatched",
            message=f"已将{scene_id or '当前'}场景交给 {agent.agent_id} 处理。",
            raw={
                "agent_task_id": task_id,
                "status": "dispatched",
                "agent_id": agent.agent_id,
                "scene_id": scene_id,
            },
        )
