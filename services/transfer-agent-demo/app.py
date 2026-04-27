from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_ROUTER_BASE_URL = "http://127.0.0.1:8024"
SKILL_PATH = Path(__file__).resolve().parent / "skills" / "transfer.skill.md"


class TransferAgentTurnRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    session_id: str = Field(alias="sessionId", min_length=1)
    task_id: str = Field(alias="taskId", min_length=1)
    message: str = Field(min_length=1)
    router_base_url: str = Field(default=DEFAULT_ROUTER_BASE_URL, alias="routerBaseUrl")

    @model_validator(mode="after")
    def normalize(self) -> "TransferAgentTurnRequest":
        self.message = self.message.strip()
        self.router_base_url = self.router_base_url.rstrip("/") or DEFAULT_ROUTER_BASE_URL
        return self


@dataclass(slots=True)
class TransferTaskMemory:
    session_id: str
    task_id: str
    recipient: str | None = None
    amount: str | None = None
    currency: str = "CNY"
    amount_source: str | None = None
    skill_step: str = "start"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "recipient": self.recipient,
            "amount": self.amount,
            "currency": self.currency,
            "amount_source": self.amount_source,
            "skill_step": self.skill_step,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TransferAgentLLMSettings:
    api_base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = 30.0
    temperature: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    structured_output_method: str = "json_mode"

    @property
    def ready(self) -> bool:
        return bool(self.api_base_url and self.model)

    @classmethod
    def from_env(cls) -> "TransferAgentLLMSettings":
        return cls(
            api_base_url=_env_first("TRANSFER_AGENT_LLM_API_BASE_URL", "ROUTER_V4_LLM_API_BASE_URL", "ROUTER_LLM_API_BASE_URL"),
            api_key=_env_first("TRANSFER_AGENT_LLM_API_KEY", "ROUTER_V4_LLM_API_KEY", "ROUTER_LLM_API_KEY"),
            model=_env_first("TRANSFER_AGENT_LLM_MODEL", "ROUTER_V4_LLM_MODEL", "ROUTER_LLM_RECOGNIZER_MODEL", "ROUTER_LLM_MODEL"),
            timeout_seconds=_positive_float("TRANSFER_AGENT_LLM_TIMEOUT_SECONDS", _positive_float("ROUTER_V4_LLM_TIMEOUT_SECONDS", 30.0)),
            temperature=_float_value("TRANSFER_AGENT_LLM_TEMPERATURE", _float_value("ROUTER_V4_LLM_TEMPERATURE", 0.0)),
            headers=_json_headers("TRANSFER_AGENT_LLM_HEADERS_JSON") or _json_headers("ROUTER_V4_LLM_HEADERS_JSON"),
            structured_output_method=_env_first(
                "TRANSFER_AGENT_LLM_STRUCTURED_OUTPUT_METHOD",
                "ROUTER_V4_LLM_STRUCTURED_OUTPUT_METHOD",
            )
            or "json_mode",
        )


@dataclass(frozen=True, slots=True)
class SkillDecision:
    task_supported: bool
    action: str
    required_slots_complete: bool | None = None
    confirmation_observed: bool | None = None
    slots_patch: dict[str, Any] = field(default_factory=dict)
    assistant_message: str = ""
    reason: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SkillDecision":
        slots_patch = payload.get("slots_patch")
        return cls(
            task_supported=payload.get("task_supported") is not False,
            action=str(payload.get("action") or "ask_missing"),
            required_slots_complete=payload.get("required_slots_complete")
            if isinstance(payload.get("required_slots_complete"), bool)
            else None,
            confirmation_observed=payload.get("confirmation_observed")
            if isinstance(payload.get("confirmation_observed"), bool)
            else None,
            slots_patch=dict(slots_patch) if isinstance(slots_patch, dict) else {},
            assistant_message=str(payload.get("assistant_message") or ""),
            reason=str(payload.get("reason") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_supported": self.task_supported,
            "action": self.action,
            "required_slots_complete": self.required_slots_complete,
            "confirmation_observed": self.confirmation_observed,
            "slots_patch": dict(self.slots_patch),
            "assistant_message": self.assistant_message,
            "reason": self.reason,
        }


class TransferSkillExecutor:
    async def decide(
        self,
        *,
        message: str,
        memory: TransferTaskMemory,
        skill_content: str,
        task_payload: dict[str, Any],
    ) -> SkillDecision:
        raise NotImplementedError


class LLMTransferSkillExecutor(TransferSkillExecutor):
    def __init__(self, settings: TransferAgentLLMSettings | None = None) -> None:
        self.settings = settings or TransferAgentLLMSettings.from_env()

    async def decide(
        self,
        *,
        message: str,
        memory: TransferTaskMemory,
        skill_content: str,
        task_payload: dict[str, Any],
    ) -> SkillDecision:
        if not self.settings.ready:
            raise RuntimeError("Transfer Agent LLM settings are incomplete")
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 transfer-agent 的 Skill 执行器。必须严格依据 transfer.skill.md、Router task snapshot、"
                        "business_context、当前 task memory 和用户本轮输入推进业务状态。"
                        "不得使用外部知识扩展业务规则，不得执行真实转账 API，只输出 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": message,
                            "skill_markdown": skill_content,
                            "task_payload": task_payload,
                            "task_memory": memory.to_dict(),
                            "output_schema": {
                                "task_supported": "boolean",
                                "action": "ask_missing|ask_confirmation|submit|cancel|handover",
                                "required_slots_complete": "boolean|null",
                                "confirmation_observed": "boolean|null",
                                "slots_patch": {
                                    "recipient": "string|null",
                                    "amount": "string|null",
                                    "currency": "string|null",
                                    "amount_source": "user_message|business_memory|null",
                                },
                                "assistant_message": "string",
                                "reason": "short Chinese reason",
                            },
                            "contract_notes": [
                                "slots_patch 只写本轮可以确定或从 business_context 明确继承的字段。",
                                "required_slots_complete 表示转账必填字段 recipient 和 amount 是否已经齐备。",
                                "required_slots_complete=true 时不要 action=ask_missing，应 action=ask_confirmation 或 submit。",
                                "required_slots_complete=false 时不要 action=ask_confirmation 或 submit。",
                                "如果 task_memory.skill_step=waiting_confirmation 且用户本轮明确确认，confirmation_observed=true，action 必须是 submit。",
                                "如果用户没有确认，confirmation_observed=false，不要 submit。",
                                "用户引用上一笔金额时，从 business_context.last_completed_for_same_scene.data.amount 继承，并设置 amount_source=business_memory。",
                                "收款人和金额完整后只能 action=ask_confirmation，除非 task_memory.skill_step 已经是 waiting_confirmation 且用户本轮明确确认。",
                                "assistant_message 中提到的已确定收款人或金额必须同步写入 slots_patch；不要话术确认了字段但 slots_patch 留空。",
                                "执行完成前必须经过确认；确认后才可以 action=submit。",
                                "任务不属于转账时 action=handover 且 task_supported=false。",
                            ],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": self.settings.temperature,
            "stream": False,
        }
        if self.settings.structured_output_method == "json_mode":
            request["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
            response = await client.post(
                _chat_completions_url(self.settings.api_base_url or ""),
                headers=_headers(self.settings),
                json=request,
            )
            if response.is_error:
                raise RuntimeError(f"Transfer Agent LLM HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
        parsed = _extract_json_object(_completion_content(payload))
        if not isinstance(parsed, dict):
            raise RuntimeError("Transfer Agent LLM response must be a JSON object")
        return SkillDecision.from_payload(parsed)


class TransferAgentRuntime:
    def __init__(self, skill_executor: TransferSkillExecutor | None = None) -> None:
        self._tasks: dict[tuple[str, str], TransferTaskMemory] = {}
        self.skill_executor = skill_executor or LLMTransferSkillExecutor()

    async def handle_turn(self, request: TransferAgentTurnRequest) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        router_task = await self._load_router_task(request, events)
        task_payload = router_task.get("task") if isinstance(router_task, dict) else None
        if not self._is_transfer_task(task_payload):
            router_update = await self._post_handover(request, task_payload, events)
            return self._response(
                status="handover",
                assistant_message="这个任务不属于转账办理，我已转交兜底处理。",
                memory=None,
                router_update=router_update,
                events=events,
                flow_nodes=self._flow_nodes(
                    memory=None,
                    events=events,
                    current_title="任务不属于转账 Agent",
                    current_summary="transfer-agent 返回 ishandover=true 且 output.data=[]。",
                    router_update=router_update,
                ),
            )

        memory = self._memory_for(request)
        skill_content = self._load_skill(events)
        assistant_message, status, router_update = await self._advance(
            request=request,
            memory=memory,
            skill_content=skill_content,
            task_payload=task_payload,
            events=events,
        )
        memory.updated_at = time.time()
        return self._response(
            status=status,
            assistant_message=assistant_message,
            memory=memory,
            router_update=router_update,
            events=events,
            flow_nodes=self._flow_nodes(
                memory=memory,
                events=events,
                current_title=self._title_for_step(memory.skill_step),
                current_summary=assistant_message,
                router_update=router_update,
            ),
        )

    async def _load_router_task(
        self,
        request: TransferAgentTurnRequest,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        url = f"{request.router_base_url}/api/router/v4/sessions/{request.session_id}/tasks/{request.task_id}"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        events.append(
            {
                "type": "agent.router_task_loaded",
                "service": "transfer-agent",
                "phase": "load",
                "title": "读取 Router 任务快照",
                "summary": "Agent 通过 HTTP 读取 Router 派发任务、原始表达和 Skill 引用。",
                "artifact": {"url": url},
                "output": payload,
            }
        )
        return payload

    def _load_skill(self, events: list[dict[str, Any]]) -> str:
        content = SKILL_PATH.read_text(encoding="utf-8")
        events.append(
            {
                "type": "agent.skill_loaded",
                "service": "transfer-agent",
                "phase": "load",
                "title": "加载转账 Skill",
                "summary": "Agent 渐进加载自己的 Skill，确定补槽、确认、风控和 API 执行边界。",
                "artifact": {"path": str(SKILL_PATH), "chars": len(content)},
                "output": content,
            }
        )
        return content

    def _memory_for(self, request: TransferAgentTurnRequest) -> TransferTaskMemory:
        key = (request.session_id, request.task_id)
        if key not in self._tasks:
            self._tasks[key] = TransferTaskMemory(session_id=request.session_id, task_id=request.task_id)
        return self._tasks[key]

    async def _advance(
        self,
        *,
        request: TransferAgentTurnRequest,
        memory: TransferTaskMemory,
        skill_content: str,
        task_payload: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> tuple[str, str, dict[str, Any] | None]:
        message = request.message.strip()
        decision = await self.skill_executor.decide(
            message=message,
            memory=memory,
            skill_content=skill_content,
            task_payload=task_payload,
        )
        events.append(_skill_decision_event(decision=decision, memory=memory))
        if not decision.task_supported or decision.action == "handover":
            router_update = await self._post_handover(request, task_payload, events)
            return "这个任务不属于转账办理，我已转交兜底处理。", "handover", router_update

        collected = self._apply_slots_patch(memory=memory, slots_patch=decision.slots_patch)
        if collected:
            events.append(_slots_event(collected, memory))

        action = self._effective_action(decision=decision, memory=memory)

        if action == "cancel":
            memory.skill_step = "cancelled"
            router_update = await self._post_agent_output(
                request=request,
                status="cancelled",
                output={
                    "data": [],
                    "agent": "transfer-agent",
                    "skill_id": "transfer",
                    "reason": "user_cancelled",
                },
                events=events,
            )
            return "已取消本次转账。", "cancelled", router_update

        missing_fields = self._missing_fields(memory)
        if action == "submit":
            if self._missing_fields(memory):
                memory.skill_step = "collecting_transfer_fields"
                return decision.assistant_message or self._missing_message(memory), "running", None
            if memory.skill_step != "waiting_confirmation":
                memory.skill_step = "waiting_confirmation"
                return decision.assistant_message or self._confirm_message(memory), "running", None
            memory.skill_step = "completed"
            router_update = await self._submit_transfer(request=request, memory=memory, events=events)
            return self._success_message(memory), "completed", router_update

        if missing_fields:
            memory.skill_step = "collecting_transfer_fields"
            events.append(
                _lifecycle_event(
                    "缺少转账信息",
                    f"按照 Skill 一次性追问缺失字段：{'、'.join(missing_fields)}。",
                    memory,
                    skill_content,
                )
            )
            return decision.assistant_message or self._missing_message(memory), "running", None

        memory.skill_step = "waiting_confirmation"
        events.append(_lifecycle_event("等待确认", "收款人和金额齐备，必须先让用户确认。", memory, skill_content))
        return decision.assistant_message or self._confirm_message(memory), "running", None

    def _effective_action(self, *, decision: SkillDecision, memory: TransferTaskMemory) -> str:
        action = decision.action
        if (
            action == "ask_confirmation"
            and decision.confirmation_observed is True
            and memory.skill_step == "waiting_confirmation"
        ):
            return "submit"
        if action == "submit" and decision.confirmation_observed is False:
            return "ask_confirmation"
        return action

    def _apply_slots_patch(self, *, memory: TransferTaskMemory, slots_patch: dict[str, Any]) -> dict[str, str]:
        collected: dict[str, str] = {}
        recipient = _normalize_text(slots_patch.get("recipient"))
        amount = _normalize_text(slots_patch.get("amount"))
        currency = _normalize_text(slots_patch.get("currency"))
        amount_source = _normalize_text(slots_patch.get("amount_source"))
        if recipient and (memory.recipient is None or memory.skill_step == "waiting_confirmation"):
            memory.recipient = recipient
            collected["recipient"] = recipient
        if amount and (memory.amount is None or memory.skill_step == "waiting_confirmation"):
            memory.amount = amount
            memory.amount_source = amount_source or "user_message"
            collected["amount"] = amount
        if currency:
            memory.currency = currency
            collected["currency"] = currency
        return collected

    def _missing_fields(self, memory: TransferTaskMemory) -> list[str]:
        missing: list[str] = []
        if not memory.recipient:
            missing.append("收款人")
        if not memory.amount:
            missing.append("转账金额")
        return missing

    def _missing_message(self, memory: TransferTaskMemory) -> str:
        missing = self._missing_fields(memory)
        if missing == ["收款人", "转账金额"]:
            return "可以，请告诉我转给谁、转账金额是多少？"
        if missing == ["收款人"]:
            return "请告诉我要转给谁。"
        if missing == ["转账金额"]:
            return "请告诉我具体转账金额。"
        return self._confirm_message(memory)

    async def _submit_transfer(
        self,
        *,
        request: TransferAgentTurnRequest,
        memory: TransferTaskMemory,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        audit_id = "ta_" + uuid.uuid4().hex[:12]
        output = {
            "data": [
                {
                    "type": "transfer_result",
                    "status": "success",
                    "recipient": memory.recipient,
                    "amount": memory.amount,
                    "currency": memory.currency,
                    "audit_id": audit_id,
                }
            ],
            "agent": "transfer-agent",
            "skill_id": "transfer",
            "risk": {"status": "passed", "level": "normal"},
            "limit": {"status": "passed"},
            "business_api": {"name": "transfer.submit", "status": "success", "audit_id": audit_id},
        }
        events.append(
            {
                "type": "agent.business_api_completed",
                "service": "transfer-agent",
                "phase": "execute",
                "title": "转账业务 API 执行",
                "summary": "已完成 demo 级风控、限额和 transfer.submit adapter 调用。",
                "output": output,
            }
        )
        return await self._post_agent_output(request=request, status="completed", output=output, events=events)

    async def _post_handover(
        self,
        request: TransferAgentTurnRequest,
        task_payload: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        return await self._post_agent_output(
            request=request,
            status="completed",
            output={
                "data": [],
                "agent": "transfer-agent",
                "skill_id": "transfer",
                "reason": "task_not_supported",
                "received_task": task_payload,
            },
            events=events,
            ishandover=True,
        )

    async def _post_agent_output(
        self,
        *,
        request: TransferAgentTurnRequest,
        status: str,
        output: dict[str, Any],
        events: list[dict[str, Any]],
        ishandover: bool | None = None,
    ) -> dict[str, Any]:
        url = f"{request.router_base_url}/api/router/v4/agent-output"
        payload: dict[str, Any] = {
            "session_id": request.session_id,
            "task_id": request.task_id,
            "status": status,
            "output": output,
        }
        if ishandover is not None:
            payload["ishandover"] = ishandover
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            router_update = response.json()
        events.append(
            {
                "type": "agent.router_callback",
                "service": "transfer-agent",
                "phase": "callback",
                "title": "Agent Output 回写 Router",
                "summary": "Agent 通过 Router 回调协议提交结构化结果。",
                "artifact": {"url": url},
                "input": payload,
                "output": router_update,
            }
        )
        return router_update

    def _response(
        self,
        *,
        status: str,
        assistant_message: str,
        memory: TransferTaskMemory | None,
        router_update: dict[str, Any] | None,
        events: list[dict[str, Any]],
        flow_nodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": status,
            "assistant_message": assistant_message,
            "agent_state": memory.to_dict() if memory is not None else {},
            "router_update": router_update,
            "events": events,
            "flow_nodes": flow_nodes,
        }

    def _flow_nodes(
        self,
        *,
        memory: TransferTaskMemory | None,
        events: list[dict[str, Any]],
        current_title: str,
        current_summary: str,
        router_update: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        task_event = _first_event(events, "agent.router_task_loaded")
        skill_event = _first_event(events, "agent.skill_loaded")
        decision_event = _first_event(events, "agent.skill_llm_decision")
        nodes = [
            {
                "id": "agent-load-task",
                "type": "agent",
                "title": "Agent 读取 Router 任务",
                "summary": "转账 Agent 通过 Router task snapshot 获取 intent、scene、Skill 引用和 task_id。",
                "status": "已加载",
                "owner": "transfer-agent",
                "details": ["这是执行 Agent 自己加载，不是 Router 内部执行。"],
                "evidence": [task_event] if task_event else [],
                "payload": task_event.get("output") if task_event else None,
            },
            {
                "id": "agent-load-skill",
                "type": "skill",
                "title": "Agent 加载转账 Skill",
                "summary": "Skill 决定业务生命周期：缺槽、确认、风控、限额、API 和回写。",
                "status": "已加载",
                "owner": "transfer-agent",
                "details": [f"文件：{SKILL_PATH}"],
                "evidence": [skill_event] if skill_event else [],
                "payload": skill_event.get("output") if skill_event else None,
            },
            {
                "id": "agent-llm-skill-decision",
                "type": "llm",
                "title": "LLM 按 Skill 推进任务",
                "summary": "LLM 消费 Skill、Router task snapshot、business_context 和 task memory，输出结构化执行决策。",
                "status": "已决策" if decision_event else "未执行",
                "owner": "transfer-agent",
                "details": ["代码只校验结构化决策并落状态，不用本地规则提槽。"],
                "evidence": [decision_event] if decision_event else [],
                "payload": decision_event.get("output") if decision_event else None,
            },
            {
                "id": "agent-current-step",
                "type": "agent",
                "title": current_title,
                "summary": current_summary,
                "status": memory.skill_step if memory is not None else "handover",
                "owner": "transfer-agent",
                "details": [
                    f"收款人：{memory.recipient or '未收集'}" if memory is not None else "无 Agent 状态",
                    f"金额：{memory.amount or '未收集'}" if memory is not None else "无 Agent 状态",
                    "确认前不会执行转账 API。",
                ],
                "evidence": events[-4:],
                "payload": memory.to_dict() if memory is not None else None,
            },
        ]
        if router_update is not None:
            nodes.append(
                {
                    "id": "agent-callback-router",
                    "type": "result",
                    "title": "业务结果回写 Router",
                    "summary": "Agent 完成后只把结构化结果交回 Router，由助手生成最终用户可见结果。",
                    "status": router_update.get("status", "task_updated"),
                    "owner": "transfer-agent",
                    "details": ["Router 不执行转账，只记录 Agent output。"],
                    "evidence": [_first_event(events, "agent.router_callback")],
                    "payload": router_update,
                }
            )
        return nodes

    def _title_for_step(self, step: str) -> str:
        return {
            "collecting_transfer_fields": "Agent 补齐转账信息",
            "waiting_confirmation": "Agent 请求用户确认",
            "completed": "转账执行完成",
            "cancelled": "转账已取消",
        }.get(step, "Agent 执行转账 Skill")

    def _confirm_message(self, memory: TransferTaskMemory) -> str:
        return f"请确认：向{memory.recipient}转账{memory.amount}元。确认办理吗？"

    def _success_message(self, memory: TransferTaskMemory) -> str:
        return f"转账成功，已向{memory.recipient}转账{memory.amount}元。"

    def _is_transfer_task(self, task_payload: Any) -> bool:
        if not isinstance(task_payload, dict):
            return False
        return task_payload.get("scene_id") == "transfer" and task_payload.get("target_agent") == "transfer-agent"


def _normalize_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _slots_event(slots: dict[str, str], memory: TransferTaskMemory) -> dict[str, Any]:
    return {
        "type": "agent.slots_collected",
        "service": "transfer-agent",
        "phase": "execute",
        "title": "LLM Skill 决策写入转账信息",
        "summary": "Agent 将 LLM 基于 Skill 输出的 slots_patch 校验后写入 task memory。",
        "output": {"slots": dict(slots), "agent_state": memory.to_dict()},
    }


def _skill_decision_event(*, decision: SkillDecision, memory: TransferTaskMemory) -> dict[str, Any]:
    return {
        "type": "agent.skill_llm_decision",
        "service": "transfer-agent",
        "phase": "execute",
        "title": "LLM 生成 Skill 执行决策",
        "summary": decision.reason or f"action={decision.action}",
        "output": {"decision": decision.to_dict(), "agent_state_before_apply": memory.to_dict()},
    }


def _lifecycle_event(title: str, summary: str, memory: TransferTaskMemory, skill_content: str) -> dict[str, Any]:
    return {
        "type": "agent.skill_lifecycle",
        "service": "transfer-agent",
        "phase": "execute",
        "title": title,
        "summary": summary,
        "output": {"agent_state": memory.to_dict(), "skill_excerpt": skill_content[:500]},
    }


def _first_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("type") == event_type:
            return event
    return None


def _completion_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError("Transfer Agent LLM response payload must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("Transfer Agent LLM response does not contain choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Transfer Agent LLM response does not contain message content")
    content = message.get("content")
    return content if isinstance(content, str) else str(content or "")


def _extract_json_object(raw: str) -> Any:
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(f"Transfer Agent LLM did not return JSON: {raw[:200]}")
    return json.loads(text[start : end + 1])


def _headers(settings: TransferAgentLLMSettings) -> dict[str, str]:
    headers = {"content-type": "application/json", **settings.headers}
    if settings.api_key:
        headers.setdefault("Authorization", f"Bearer {settings.api_key}")
    return headers


def _chat_completions_url(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_value(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _json_headers(name: str) -> dict[str, str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def _load_env_file(path: str | Path | None) -> None:
    if path is None:
        return
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


_load_env_file(os.environ.get("TRANSFER_AGENT_ENV_FILE") or os.environ.get("ROUTER_V4_ENV_FILE") or ".env.local")
runtime = TransferAgentRuntime()

app = FastAPI(
    title="Transfer Agent Demo Service",
    version="0.1.0",
    default_response_class=ORJSONResponse,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3010",
        "http://127.0.0.1:3010",
        "http://localhost:3011",
        "http://127.0.0.1:3011",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/transfer-agent/turn", response_model=None)
async def post_turn(request: TransferAgentTurnRequest) -> dict[str, Any]:
    return await runtime.handle_turn(request)
