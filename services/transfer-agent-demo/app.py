from __future__ import annotations

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
CONFIRM_WORDS = {"确认", "确定", "是", "可以", "同意", "继续", "提交", "办理"}
CANCEL_WORDS = {"取消", "算了", "不转了", "先不转", "停止", "退出"}


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
            "skill_step": self.skill_step,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TransferAgentRuntime:
    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], TransferTaskMemory] = {}

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

        memory = self._memory_for(request, task_payload)
        skill_content = self._load_skill(events)
        self._bootstrap_from_router_slots(memory, task_payload)
        assistant_message, status, router_update = await self._advance(
            request=request,
            memory=memory,
            skill_content=skill_content,
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
                "summary": "Agent 通过 HTTP 读取 Router 派发任务和 routing slot hints。",
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

    def _memory_for(self, request: TransferAgentTurnRequest, task_payload: dict[str, Any] | None) -> TransferTaskMemory:
        key = (request.session_id, request.task_id)
        if key not in self._tasks:
            self._tasks[key] = TransferTaskMemory(session_id=request.session_id, task_id=request.task_id)
        memory = self._tasks[key]
        if task_payload:
            slots = _as_dict(task_payload.get("routing_slots"))
            if memory.recipient is None and slots.get("recipient"):
                memory.recipient = str(slots["recipient"])
            if memory.amount is None and slots.get("amount"):
                memory.amount = _normalize_amount(slots["amount"])
        return memory

    def _bootstrap_from_router_slots(self, memory: TransferTaskMemory, task_payload: dict[str, Any] | None) -> None:
        if memory.skill_step != "start" or not task_payload:
            return
        slots = _as_dict(task_payload.get("routing_slots"))
        if slots.get("recipient") and memory.recipient is None:
            memory.recipient = str(slots["recipient"])
        if slots.get("amount") and memory.amount is None:
            memory.amount = _normalize_amount(slots["amount"])

    async def _advance(
        self,
        *,
        request: TransferAgentTurnRequest,
        memory: TransferTaskMemory,
        skill_content: str,
        events: list[dict[str, Any]],
    ) -> tuple[str, str, dict[str, Any] | None]:
        message = request.message.strip()
        if _is_cancel(message):
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

        if memory.skill_step == "waiting_confirmation":
            if not _is_confirm(message):
                self._collect_message_slots(message=message, memory=memory, events=events)
                if self._missing_fields(memory):
                    memory.skill_step = "collecting_transfer_fields"
                    return self._missing_message(memory), "running", None
                if _extract_transfer_slots(message):
                    return self._confirm_message(memory), "running", None
                return "请回复“确认”继续办理，或回复“取消”结束本次转账。", "running", None
            if self._missing_fields(memory):
                memory.skill_step = "collecting_transfer_fields"
                return self._missing_message(memory), "running", None
            memory.skill_step = "completed"
            router_update = await self._submit_transfer(request=request, memory=memory, events=events)
            return self._success_message(memory), "completed", router_update

        self._collect_message_slots(message=message, memory=memory, events=events)
        missing_fields = self._missing_fields(memory)
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
            return self._missing_message(memory), "running", None

        memory.skill_step = "waiting_confirmation"
        events.append(_lifecycle_event("等待确认", "收款人和金额齐备，必须先让用户确认。", memory, skill_content))
        return self._confirm_message(memory), "running", None

    def _collect_message_slots(
        self,
        *,
        message: str,
        memory: TransferTaskMemory,
        events: list[dict[str, Any]],
    ) -> None:
        slots = _extract_transfer_slots(message)
        collected: dict[str, str] = {}
        recipient = slots.get("recipient")
        amount = slots.get("amount")
        if recipient and (memory.recipient is None or memory.skill_step == "waiting_confirmation"):
            memory.recipient = recipient
            collected["recipient"] = recipient
        if amount and (memory.amount is None or memory.skill_step == "waiting_confirmation"):
            memory.amount = amount
            collected["amount"] = amount
        if collected:
            events.append(_slots_event(collected, memory))

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
                "summary": "已完成模拟风控、限额和 transfer.submit。",
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
        nodes = [
            {
                "id": "agent-load-task",
                "type": "agent",
                "title": "Agent 读取 Router 任务",
                "summary": "转账 Agent 通过 Router task snapshot 获取 scene、routing_slots 和 task_id。",
                "status": "已加载",
                "owner": "transfer-agent",
                "details": ["这是执行 Agent 自己加载，不是 Router 内部模拟。"],
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


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_amount(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _extract_amount(message: str) -> str | None:
    translated = message.translate(str.maketrans("０１２３４５６７８９．，", "0123456789.,"))
    current: list[str] = []
    numbers: list[str] = []
    for char in translated:
        if char.isdigit() or (char == "." and current and "." not in current):
            current.append(char)
        elif current:
            numbers.append("".join(current))
            current = []
    if current:
        numbers.append("".join(current))
    for number in numbers:
        if any(char.isdigit() for char in number):
            return number.rstrip(".")
    return None


def _extract_transfer_slots(message: str) -> dict[str, str]:
    amount = _extract_amount(message)
    recipient = _extract_recipient(message, amount)
    slots: dict[str, str] = {}
    if recipient:
        slots["recipient"] = recipient
    if amount:
        slots["amount"] = amount
    return slots


def _extract_recipient(message: str, amount: str | None) -> str:
    text = message.strip()
    if amount:
        text = text.replace(amount, "")
    for token in (
        "人民币",
        "元",
        "块钱",
        "块",
        "我要",
        "我想",
        "帮我",
        "请帮我",
        "请",
        "转账给",
        "转给",
        "转账",
        "汇款给",
        "汇款",
        "打款给",
        "打款",
        "打钱给",
        "打钱",
        "转",
        "给",
        "向",
        "收款人是",
        "收款人",
        "是",
    ):
        text = text.replace(token, "")
    for char in "，。！？、,.!? ：:;；\t\n\r":
        text = text.replace(char, "")
    if not text or _normalize_decision(text) in CONFIRM_WORDS or _normalize_decision(text) in CANCEL_WORDS:
        return ""
    if _extract_amount(text) == text:
        return ""
    return text[:32]


def _normalize_decision(message: str) -> str:
    text = message.strip()
    for char in "，。！？、,.!? ":
        text = text.replace(char, "")
    return text


def _is_confirm(message: str) -> bool:
    return _normalize_decision(message) in CONFIRM_WORDS


def _is_cancel(message: str) -> bool:
    return _normalize_decision(message) in CANCEL_WORDS


def _slots_event(slots: dict[str, str], memory: TransferTaskMemory) -> dict[str, Any]:
    return {
        "type": "agent.slots_collected",
        "service": "transfer-agent",
        "phase": "execute",
        "title": "自由表达提取转账信息",
        "summary": "Agent 从用户自然表达中一次性提取可获得的转账字段。",
        "output": {"slots": dict(slots), "agent_state": memory.to_dict()},
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
