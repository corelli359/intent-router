from __future__ import annotations

import asyncio
import json
from json import JSONDecodeError
import os
from pathlib import Path
from dataclasses import dataclass
import logging
from typing import Any, Literal, Protocol

import httpx
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger(__name__)
ENV_FILENAMES = (".env", ".env.local")


def _env_search_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[Path] = set()

    for candidate in (Path.cwd(), Path("/workspace")):
        resolved = candidate.expanduser().resolve()
        if not resolved.exists() or resolved in seen:
            continue
        roots.append(resolved)
        seen.add(resolved)

    for parent in Path(__file__).resolve().parents:
        if parent in seen:
            continue
        roots.append(parent)
        seen.add(parent)
        if (parent / ".git").exists() or (parent / "AGENTS.md").is_file():
            break
    return tuple(roots)


def _load_local_env_files() -> None:
    for root in _env_search_roots():
        for filename in ENV_FILENAMES:
            env_path = root / filename
            if not env_path.is_file():
                continue
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line.removeprefix("export ").strip()
                if "=" not in line:
                    continue
                key, raw_value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ:
                    continue
                value = raw_value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                os.environ[key] = value


def _env_headers(name: str) -> dict[str, str]:
    raw_value = os.getenv(name)
    if not raw_value:
        return {}
    parsed = json.loads(raw_value)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return {str(key): str(value) for key, value in parsed.items()}


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


def llm_exception_is_retryable(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 429


class AgentCustomer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cust_id: str | None = Field(default=None, alias="custId")


class ConfigVariablesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="session_id")
    txt: str = ""
    stream: bool = True
    config_variables: list[dict[str, str]] = Field(default_factory=list)

    def get_config_value(self, name: str, default: str = "") -> str:
        for item in self.config_variables:
            if item.get("name") == name:
                return item.get("value", default)
        return default

    def get_slots_data(self) -> dict[str, Any]:
        raw = self.get_config_value("slots_data")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
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
    llm_rate_limit_max_retries: int = Field(default=2, ge=0)
    llm_rate_limit_retry_delay_seconds: float = Field(default=2.0, gt=0)
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
            llm_rate_limit_max_retries=int(
                _env_first(f"{prefix}_LLM_RATE_LIMIT_MAX_RETRIES", "ROUTER_LLM_RATE_LIMIT_MAX_RETRIES") or "2"
            ),
            llm_rate_limit_retry_delay_seconds=_env_float(
                2.0,
                f"{prefix}_LLM_RATE_LIMIT_RETRY_DELAY_SECONDS",
                "ROUTER_LLM_RATE_LIMIT_RETRY_DELAY_SECONDS",
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
        structured_error: Exception | None = None
        if schema is not None:
            try:
                structured_chain = prompt | model.with_structured_output(
                    schema,
                    method="json_mode",
                )
                response = await self._ainvoke_with_retry(structured_chain, variables)
                if isinstance(response, BaseModel):
                    return response.model_dump()
                if response:
                    return response
            except Exception as exc:
                structured_error = exc
                if llm_exception_is_retryable(exc):
                    raise

        chain = prompt | model
        chunks: list[str] = []
        async for chunk in self._astream_with_retry(chain, variables):
            text = _chunk_text(chunk.content)
            if text:
                chunks.append(text)
        payload = extract_json_value("".join(chunks))
        if schema is None:
            return payload
        try:
            validated = schema.model_validate(payload)
        except Exception:
            if structured_error is not None:
                raise structured_error
            raise
        return validated.model_dump()

    async def _ainvoke_with_retry(self, chain, variables: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_rate_limit_max_retries + 1):
            try:
                return await chain.ainvoke(variables)
            except Exception as exc:
                if attempt >= self.settings.llm_rate_limit_max_retries or not llm_exception_is_retryable(exc):
                    raise
                last_error = exc
                delay = min(
                    self.settings.llm_rate_limit_retry_delay_seconds * (attempt + 1),
                    self.settings.llm_timeout_seconds,
                )
                logger.warning(
                    "Retrying agent LLM structured call after transient rate limit (%s/%s) in %.2fs",
                    attempt + 1,
                    self.settings.llm_rate_limit_max_retries,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Agent structured LLM invocation failed without raising an error")

    async def _astream_with_retry(self, chain, variables: dict[str, Any]):
        last_error: Exception | None = None
        for attempt in range(self.settings.llm_rate_limit_max_retries + 1):
            try:
                async for chunk in chain.astream(variables):
                    yield chunk
                return
            except Exception as exc:
                if attempt >= self.settings.llm_rate_limit_max_retries or not llm_exception_is_retryable(exc):
                    raise
                last_error = exc
                delay = min(
                    self.settings.llm_rate_limit_retry_delay_seconds * (attempt + 1),
                    self.settings.llm_timeout_seconds,
                )
                logger.warning(
                    "Retrying agent LLM streaming call after transient rate limit (%s/%s) in %.2fs",
                    attempt + 1,
                    self.settings.llm_rate_limit_max_retries,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Agent streaming LLM invocation failed without raising an error")


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
