from __future__ import annotations

import json
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any, Awaitable, Callable, Literal, Protocol

import httpx
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from router_core.domain import Task


AsyncDeltaCallback = Callable[[str], Awaitable[None]]


def extract_json_value(raw_text: str) -> Any:
    text = raw_text.strip()
    if not text:
        raise ValueError("LLM response is empty")

    try:
        return json.loads(text)
    except JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
            return value
        except JSONDecodeError:
            continue
    raise ValueError(f"Could not find JSON payload in LLM response: {raw_text[:200]}")


class IntentRecognitionMatchPayload(BaseModel):
    intent_code: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = "llm returned a match"

    @model_validator(mode="before")
    @classmethod
    def normalize_match(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "confidence" not in normalized:
            if "score" in normalized:
                normalized["confidence"] = normalized["score"]
            elif "probability" in normalized:
                normalized["confidence"] = normalized["probability"]
        normalized.setdefault("reason", "llm returned a match")
        return normalized


class IntentRecognitionPayload(BaseModel):
    matches: list[IntentRecognitionMatchPayload] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> Any:
        if isinstance(value, list):
            return {"matches": value}
        if isinstance(value, dict) and "matches" not in value:
            for key in ("intents", "results", "items"):
                candidate = value.get(key)
                if isinstance(candidate, list):
                    normalized = dict(value)
                    normalized["matches"] = candidate
                    return normalized
        return value


class IntentAgentPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["waiting_user_input", "completed", "failed"]
    event: Literal["message", "final"] = "final"
    ishandover: bool
    content: str = Field(validation_alias=AliasChoices("content", "response", "message"))
    slot_memory: dict[str, Any] = Field(default_factory=dict, validation_alias=AliasChoices("slot_memory", "slots"))
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)

        if "content" not in normalized:
            for key in ("response", "message", "text"):
                if key in normalized:
                    normalized["content"] = normalized[key]
                    break

        if "slot_memory" not in normalized and "slots" in normalized:
            normalized["slot_memory"] = normalized["slots"]

        if "status" not in normalized:
            if "ishandover" in normalized:
                normalized["status"] = "completed" if normalized["ishandover"] else "waiting_user_input"
            else:
                content = str(normalized.get("content", ""))
                if any(marker in content for marker in ("请", "?", "？")):
                    normalized["status"] = "waiting_user_input"
                    normalized.setdefault("ishandover", False)
                else:
                    normalized["status"] = "completed"
                    normalized.setdefault("ishandover", True)

        if "ishandover" not in normalized:
            normalized["ishandover"] = normalized["status"] in {"completed", "failed"}

        if "event" not in normalized:
            normalized["event"] = "final" if normalized["ishandover"] else "message"

        return normalized


class LLMClient(Protocol):
    async def recognize(
        self,
        *,
        message: str,
        recent_messages: list[str],
        long_term_memory: list[str],
        intents: list[dict[str, Any]],
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> IntentRecognitionPayload: ...

    async def run_agent(
        self,
        *,
        task: Task,
        user_input: str,
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> IntentAgentPayload: ...


@dataclass(slots=True)
class LangChainLLMClient:
    base_url: str
    default_model: str
    api_key: str | None = None
    timeout_seconds: float = 30.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = "json_mode"
    http_async_client: httpx.AsyncClient | None = None

    async def recognize(
        self,
        *,
        message: str,
        recent_messages: list[str],
        long_term_memory: list[str],
        intents: list[dict[str, Any]],
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> IntentRecognitionPayload:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "你是一个多意图识别器。"
                        "只能从已注册 intent 中选择，不能虚构新的 intent_code。"
                        "你可以返回多个意图，但必须保持谨慎。"
                        "confidence 必须在 0 到 1 之间。"
                        "如果当前消息与某个已注册意图明显匹配，不要返回空列表。"
                    ),
                ),
                (
                    "human",
                    (
                        "当前消息:\n{message}\n\n"
                        "最近对话(JSON):\n{recent_messages_json}\n\n"
                        "长期记忆(JSON):\n{long_term_memory_json}\n\n"
                        "已注册意图(JSON):\n{intents_json}"
                    ),
                ),
            ]
        )
        response_text = await self._stream_prompt(
            prompt,
            {
                "message": message,
                "recent_messages_json": json.dumps(recent_messages, ensure_ascii=False, indent=2),
                "long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False, indent=2),
                "intents_json": json.dumps(intents, ensure_ascii=False, indent=2),
            },
            model=model,
            on_delta=on_delta,
        )
        return IntentRecognitionPayload.model_validate(extract_json_value(response_text))

    async def run_agent(
        self,
        *,
        task: Task,
        user_input: str,
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> IntentAgentPayload:
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    (
                        "你是意图路由器中的单意图执行 Agent。"
                        "你只能处理当前 intent，不能扩展到未注册任务。"
                        "当信息不足时，返回 waiting_user_input 并清楚告诉用户缺什么；此时 ishandover 必须为 false。"
                        "只有任务完成或明确失败时，ishandover 才能为 true。"
                        "如果你识别到了结构化槽位，请放到 slot_memory。"
                        "回复主文本字段请使用 content。"
                    ),
                ),
                (
                    "human",
                    (
                        "当前用户输入:\n{user_input}\n\n"
                        "Intent(JSON):\n{intent_json}\n\n"
                        "最近对话(JSON):\n{recent_messages_json}\n\n"
                        "长期记忆(JSON):\n{long_term_memory_json}\n\n"
                        "已有槽位(JSON):\n{slot_memory_json}\n\n"
                        "字段映射(JSON):\n{field_mapping_json}\n\n"
                        "请求结构(JSON):\n{request_schema_json}"
                    ),
                ),
            ]
        )
        response_text = await self._stream_prompt(
            prompt,
            {
                "user_input": user_input,
                "intent_json": json.dumps(
                    {
                        "intent_code": task.intent_code,
                        "name": task.intent_name,
                        "description": task.intent_description,
                        "examples": task.intent_examples,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "recent_messages_json": json.dumps(
                    task.input_context.get("recent_messages", []),
                    ensure_ascii=False,
                    indent=2,
                ),
                "long_term_memory_json": json.dumps(
                    task.input_context.get("long_term_memory", []),
                    ensure_ascii=False,
                    indent=2,
                ),
                "slot_memory_json": json.dumps(task.slot_memory, ensure_ascii=False, indent=2, default=str),
                "field_mapping_json": json.dumps(task.field_mapping, ensure_ascii=False, indent=2),
                "request_schema_json": json.dumps(task.request_schema, ensure_ascii=False, indent=2),
            },
            model=model,
            on_delta=on_delta,
        )
        return IntentAgentPayload.model_validate(extract_json_value(response_text))

    def _create_model(self, model: str | None = None) -> ChatOpenAI:
        return ChatOpenAI(
            model_name=model or self.default_model,
            temperature=0,
            openai_api_key=self.api_key,
            openai_api_base=self.base_url,
            request_timeout=self.timeout_seconds,
            default_headers=self.extra_headers or None,
            http_async_client=self.http_async_client,
        )

    async def _stream_prompt(
        self,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        *,
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> str:
        chain = prompt | self._create_model(model)
        chunks: list[str] = []
        async for chunk in chain.astream(variables):
            text = self._chunk_text(chunk.content)
            if not text:
                continue
            chunks.append(text)
            if on_delta is not None:
                await on_delta(text)
        return "".join(chunks)

    def _chunk_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    texts.append(item)
                    continue
                if not isinstance(item, dict):
                    texts.append(str(item))
                    continue
                if item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
                    continue
                if "text" in item:
                    texts.append(str(item["text"]))
            return "".join(texts)
        return str(content or "")
