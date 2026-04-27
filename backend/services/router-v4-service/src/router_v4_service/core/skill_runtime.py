from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

import httpx

from router_v4_service.core.config import RouterV4LLMSettings


@dataclass(frozen=True, slots=True)
class SkillDecision:
    """Structured decision returned by the Skill ReAct LLM."""

    task_supported: bool
    action: str
    required_slots_complete: bool | None = None
    confirmation_observed: bool | None = None
    slots_patch: dict[str, Any] = field(default_factory=dict)
    assistant_message: str = ""
    reason: str = ""
    tool_call: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SkillDecision":
        slots_patch = payload.get("slots_patch")
        tool_call = payload.get("tool_call")
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
            tool_call=dict(tool_call) if isinstance(tool_call, dict) else {},
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
            "tool_call": dict(self.tool_call),
        }


class SkillExecutor(Protocol):
    def decide(
        self,
        *,
        user_message: str,
        skill_markdown: str,
        task_payload: dict[str, Any],
        task_memory: dict[str, Any],
    ) -> SkillDecision:
        """Return the next Skill ReAct decision."""


class LLMSkillExecutor:
    """OpenAI-compatible Skill ReAct executor driven by skill markdown."""

    def __init__(self, settings: RouterV4LLMSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client

    def decide(
        self,
        *,
        user_message: str,
        skill_markdown: str,
        task_payload: dict[str, Any],
        task_memory: dict[str, Any],
    ) -> SkillDecision:
        if not self.settings.ready:
            raise RuntimeError("Skill ReAct LLM settings are incomplete")
        request: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是银行意图框架内的 Skill ReAct Runtime。必须严格依据当前 skill.md、"
                        "Router task snapshot、business_context、task memory 和用户本轮输入推进业务。"
                        "Router 代码不能硬编码业务提槽；业务字段只能来自你的结构化决策。"
                        "不要执行真实外部 API，只输出 output_schema 允许的 JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": user_message,
                            "skill_markdown": skill_markdown,
                            "task_payload": task_payload,
                            "task_memory": task_memory,
                            "output_schema": {
                                "task_supported": "boolean",
                                "action": "ask_missing|ask_confirmation|submit|cancel|handover|tool_call",
                                "required_slots_complete": "boolean|null",
                                "confirmation_observed": "boolean|null",
                                "slots_patch": "object",
                                "assistant_message": "string",
                                "reason": "short Chinese reason",
                                "tool_call": {
                                    "name": "string|null",
                                    "arguments": "object",
                                },
                            },
                            "contract_notes": [
                                "skill.md 是业务边界；如有提槽说明，按提槽说明输出 slots_patch。",
                                "required_slots_complete 表示当前 Skill 要求的必填业务字段是否已经齐备。",
                                "required_slots_complete=true 时不要 action=ask_missing，应 action=ask_confirmation 或 submit/tool_call。",
                                "required_slots_complete=false 时不要 action=ask_confirmation 或 submit。",
                                "如果 task_memory.skill_step=waiting_confirmation 且用户本轮明确确认，confirmation_observed=true，action 必须是 submit 或 tool_call。",
                                "如果用户没有确认，confirmation_observed=false，不要 submit/tool_call。",
                                "如有工具或 API 调用说明，先输出 action=tool_call 或 submit，并在 tool_call 中声明。",
                                "执行完成前必须满足 skill.md 的确认门禁。",
                                "任务不属于本 skill 时返回 task_supported=false 且 action=handover。",
                                "assistant_message 中提到的已确定业务对象必须同步写入 slots_patch；不要话术确认了某字段但 slots_patch 留空。",
                                "不要输出 output_schema 之外的字段。",
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
        client = self.client or httpx.Client(timeout=self.settings.timeout_seconds)
        try:
            response = client.post(
                _chat_completions_url(self.settings.api_base_url or ""),
                headers=_headers(self.settings),
                json=request,
            )
            if response.is_error:
                raise RuntimeError(f"Skill ReAct LLM HTTP {response.status_code}: {response.text[:500]}")
            payload = response.json()
        finally:
            if self.client is None:
                client.close()
        parsed = _extract_json_object(_completion_content(payload))
        if not isinstance(parsed, dict):
            raise RuntimeError("Skill ReAct LLM response must be a JSON object")
        return SkillDecision.from_payload(parsed)


def _headers(settings: RouterV4LLMSettings) -> dict[str, str]:
    headers = {"content-type": "application/json", **settings.headers}
    if settings.api_key:
        headers.setdefault("Authorization", f"Bearer {settings.api_key}")
    return headers


def _chat_completions_url(base_url: str) -> str:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/chat/completions"):
        return stripped
    return f"{stripped}/chat/completions"


def _completion_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError("Skill ReAct LLM response payload must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("Skill ReAct LLM response does not contain choices")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Skill ReAct LLM response does not contain message content")
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
        raise RuntimeError(f"Skill ReAct LLM did not return JSON: {raw[:200]}")
    return json.loads(text[start : end + 1])
