from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field

from config.settings import _env_headers, _load_local_env_files
from router_core.llm_client import extract_json_value


class AgentCustomer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cust_id: str | None = Field(default=None, alias="custId")


class AgentConversationContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    recent_messages: list[str] = Field(default_factory=list, alias="recentMessages")
    long_term_memory: list[str] = Field(default_factory=list, alias="longTermMemory")


class AgentIntentContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str | None = None
    name: str | None = None
    description: str | None = None
    examples: list[str] = Field(default_factory=list)


class AgentExecutionResponse(BaseModel):
    event: Literal["message", "final"]
    content: str
    ishandover: bool
    status: Literal["waiting_user_input", "completed", "failed"]
    slot_memory: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def waiting(
        cls,
        content: str,
        *,
        slot_memory: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> "AgentExecutionResponse":
        return cls(
            event="message",
            content=content,
            ishandover=False,
            status="waiting_user_input",
            slot_memory=slot_memory or {},
            payload=payload or {},
        )

    @classmethod
    def completed(
        cls,
        content: str,
        *,
        slot_memory: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> "AgentExecutionResponse":
        return cls(
            event="final",
            content=content,
            ishandover=True,
            status="completed",
            slot_memory=slot_memory or {},
            payload=payload or {},
        )

    @classmethod
    def failed(
        cls,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> "AgentExecutionResponse":
        return cls(
            event="final",
            content=content,
            ishandover=True,
            status="failed",
            payload=payload or {},
        )


class AgentCancelRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    task_id: str = Field(alias="taskId")


class AgentCancelResponse(BaseModel):
    status: Literal["cancelled"]
    accepted: bool = True


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value
    return None


def _env_float(default: float, *names: str) -> float:
    value = _env_first(*names)
    if value is None:
        return default
    return float(value)


def _env_headers_with_fallback(primary_name: str, fallback_name: str) -> dict[str, str]:
    raw_primary = os.getenv(primary_name)
    if raw_primary:
        return _env_headers(primary_name)
    raw_fallback = os.getenv(fallback_name)
    if raw_fallback:
        return _env_headers(fallback_name)
    return {}


class AgentLLMSettings(BaseModel):
    service_name: str
    llm_api_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_headers: dict[str, str] = Field(default_factory=dict)

    @property
    def connection_ready(self) -> bool:
        return bool(self.llm_api_base_url and self.llm_model)

    @classmethod
    def from_env(cls, *, prefix: str, service_name: str) -> "AgentLLMSettings":
        _load_local_env_files()
        return cls(
            service_name=service_name,
            llm_api_base_url=_env_first(f"{prefix}_LLM_API_BASE_URL", "ROUTER_LLM_API_BASE_URL"),
            llm_api_key=_env_first(f"{prefix}_LLM_API_KEY", "ROUTER_LLM_API_KEY"),
            llm_model=_env_first(f"{prefix}_LLM_MODEL", "ROUTER_LLM_AGENT_MODEL", "ROUTER_LLM_MODEL"),
            llm_timeout_seconds=_env_float(
                30.0,
                f"{prefix}_LLM_TIMEOUT_SECONDS",
                "ROUTER_LLM_TIMEOUT_SECONDS",
            ),
            llm_headers=_env_headers_with_fallback(f"{prefix}_LLM_HEADERS_JSON", "ROUTER_LLM_HEADERS_JSON"),
        )


class JsonObjectRunner(Protocol):
    async def run_json(
        self,
        *,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        schema: type[BaseModel] | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class LangChainJsonObjectRunner:
    settings: AgentLLMSettings
    http_async_client: httpx.AsyncClient | None = None

    async def run_json(
        self,
        *,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        schema: type[BaseModel] | None = None,
    ) -> Any:
        if not self.settings.connection_ready:
            raise RuntimeError(f"{self.settings.service_name} LLM settings are incomplete")

        model = ChatOpenAI(
            model_name=self.settings.llm_model,
            temperature=0,
            openai_api_key=self.settings.llm_api_key,
            openai_api_base=self.settings.llm_api_base_url,
            request_timeout=self.settings.llm_timeout_seconds,
            default_headers=self.settings.llm_headers or None,
            http_async_client=self.http_async_client,
        )
        if schema is not None:
            structured_chain = prompt | model.with_structured_output(
                schema,
                method="json_mode",
            )
            response = await structured_chain.ainvoke(variables)
            if isinstance(response, BaseModel):
                return response.model_dump()
            return response

        chain = prompt | model
        chunks: list[str] = []
        async for chunk in chain.astream(variables):
            text = _chunk_text(chunk.content)
            if text:
                chunks.append(text)
        return extract_json_value("".join(chunks))


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _chunk_text(content: Any) -> str:
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
