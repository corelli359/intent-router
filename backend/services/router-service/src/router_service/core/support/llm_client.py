from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from dataclasses import dataclass, field
from json import JSONDecodeError
import logging
import time
from typing import Any, Awaitable, Callable, Literal, Protocol
from uuid import uuid4

import httpx
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator
from router_service.core.support.llm_barrier import build_llm_barrier_error
from router_service.core.support.jwt_utils import AuthHTTPClient
from router_service.core.support.trace_logging import current_trace_id

AsyncDeltaCallback = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)


def extract_json_value(raw_text: str) -> Any:
    """Extract the first valid JSON object or array from raw LLM output text."""
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
    """Structured LLM payload for a single intent recognition match."""

    intent_code: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = "llm returned a match"

    @model_validator(mode="before")
    @classmethod
    def normalize_match(cls, value: Any) -> Any:
        """Normalize alternate confidence field names returned by different prompts/models."""
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
    """Structured LLM payload for a batch of intent recognition matches."""

    matches: list[IntentRecognitionMatchPayload] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_payload(cls, value: Any) -> Any:
        """Normalize alternate top-level list keys returned by different prompts/models."""
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


class JsonLLMClient(Protocol):
    """Protocol for LLM clients capable of returning parsed JSON responses."""

    async def run_json(
        self,
        *,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> Any:
        """Run one prompt and return a parsed JSON-compatible Python value."""
        ...


def llm_exception_is_retryable(exc: Exception) -> bool:
    """Return whether an exception should be retried as a transient LLM failure."""
    return getattr(exc, "status_code", None) == 429


@dataclass(slots=True)
class LangChainLLMClient:
    """LangChain-based JSON LLM client with rate-limit aware retry handling."""

    base_url: str
    default_model: str
    api_key: str | None = None
    timeout_seconds: float = 30.0
    rate_limit_max_retries: int = 2
    rate_limit_retry_delay_seconds: float = 2.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = "json_mode"
    http_async_client: httpx.AsyncClient | None = None
    barrier_enabled: bool = False

    async def run_json(
        self,
        *,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> Any:
        """Run one prompt, stream its raw text, and extract JSON from the result."""
        effective_model = model or self.default_model
        call_id = uuid4().hex[:8]
        trace_id = current_trace_id()
        prompt_name = prompt.__class__.__name__
        if self.barrier_enabled:
            logger.error(
                "LLM barrier blocked call (trace_id=%s, call_id=%s, model=%s, base_url=%s, prompt=%s)",
                trace_id,
                call_id,
                effective_model,
                self.base_url,
                prompt_name,
            )
            raise build_llm_barrier_error(
                model=effective_model,
                prompt_name=prompt_name,
                base_url=self.base_url,
            )
        variable_keys = ",".join(sorted(str(key) for key in variables.keys()))
        started_at = time.perf_counter()
        logger.debug(
            "LLM call request (trace_id=%s, call_id=%s, model=%s, base_url=%s, prompt=%s, variable_keys=%s, messages=%s)",
            trace_id,
            call_id,
            effective_model,
            self.base_url,
            prompt_name,
            variable_keys,
            self._render_prompt_messages(prompt, variables),
        )
        try:
            response_text = await self._stream_prompt(prompt, variables, model=model, on_delta=on_delta)
            parsed_payload = extract_json_value(response_text)
        except Exception:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.warning(
                "LLM call failed (trace_id=%s, call_id=%s, model=%s, base_url=%s, elapsed_ms=%.2f, prompt=%s)",
                trace_id,
                call_id,
                effective_model,
                self.base_url,
                elapsed_ms,
                prompt_name,
                exc_info=True,
            )
            raise
        logger.debug(
            "LLM call response (trace_id=%s, call_id=%s, model=%s, base_url=%s, response_text=%s)",
            trace_id,
            call_id,
            effective_model,
            self.base_url,
            response_text,
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.debug(
            "LLM call completed (trace_id=%s, call_id=%s, model=%s, base_url=%s, elapsed_ms=%.2f, prompt=%s, variable_keys=%s, response_chars=%s)",
            trace_id,
            call_id,
            effective_model,
            self.base_url,
            elapsed_ms,
            prompt_name,
            variable_keys,
            len(response_text),
        )
        return parsed_payload

    def _create_model(self, model: str | None = None) -> ChatOpenAI:
        """Create the configured ChatOpenAI client instance for one request."""
        return ChatOpenAI(
            model_name=model or self.default_model,
            temperature=0,
            openai_api_key=self._effective_api_key(),
            openai_api_base=self.base_url,
            request_timeout=self.timeout_seconds,
            default_headers=self.extra_headers or None,
            http_async_client=self.http_async_client,
        )

    def _effective_api_key(self) -> str | None:
        """Return the API key expected by ChatOpenAI, using a placeholder for custom auth clients."""
        if self.api_key is not None:
            return self.api_key
        if isinstance(self.http_async_client, AuthHTTPClient):
            return "jwt-auth-placeholder"
        return ""

    async def aclose(self) -> None:
        """Close the owned async HTTP client when present."""
        if self.http_async_client is not None:
            await self.http_async_client.aclose()

    async def _stream_prompt(
        self,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        *,
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> str:
        """Stream one prompt with retry handling and return the accumulated text."""
        last_error: Exception | None = None
        for attempt in range(self.rate_limit_max_retries + 1):
            try:
                if on_delta is not None:
                    return await self._stream_once(prompt, variables, model=model, on_delta=on_delta)
                return await self._invoke_once(prompt, variables, model=model)
            except Exception as exc:
                if not self._should_retry_rate_limit(exc, attempt=attempt):
                    raise
                last_error = exc
                delay = self._rate_limit_retry_delay(exc, attempt=attempt)
                logger.debug(
                    "Retrying LLM call after transient rate limit (%s/%s) in %.2fs",
                    attempt + 1,
                    self.rate_limit_max_retries,
                    delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM prompt streaming failed without raising an error")

    async def _stream_once(
        self,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        *,
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> str:
        """Execute one non-retried streaming prompt call."""
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

    async def _invoke_once(
        self,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        *,
        model: str | None = None,
    ) -> str:
        """Execute one non-retried non-streaming prompt call."""
        chain = prompt | self._create_model(model)
        result = await chain.ainvoke(variables)
        return self._chunk_text(result.content)

    def _should_retry_rate_limit(self, exc: Exception, *, attempt: int) -> bool:
        """Return whether one failed attempt should be retried as a rate-limit error."""
        if attempt >= self.rate_limit_max_retries:
            return False
        return llm_exception_is_retryable(exc)

    def _rate_limit_retry_delay(self, exc: Exception, *, attempt: int) -> float:
        """Derive the next retry delay from provider metadata or local backoff."""
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                reset_time = error.get("reset_time")
                if isinstance(reset_time, str):
                    try:
                        reset_at = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                    except ValueError:
                        reset_at = None
                    if reset_at is not None:
                        delay = (reset_at - datetime.now(timezone.utc)).total_seconds() + 0.25
                        if delay > 0:
                            return min(delay, self.timeout_seconds)
        return min(self.rate_limit_retry_delay_seconds * (attempt + 1), self.timeout_seconds)

    def _chunk_text(self, content: Any) -> str:
        """Normalize streamed chunk content into plain text."""
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

    def _render_prompt_messages(self, prompt: ChatPromptTemplate, variables: dict[str, Any]) -> str:
        """Render the final prompt messages into a JSON log payload."""
        try:
            rendered_messages = prompt.format_messages(**variables)
        except Exception as exc:
            return json.dumps(
                [{"role": "render_error", "content": f"prompt render failed: {exc}"}],
                ensure_ascii=False,
            )
        serialized_messages = [
            {
                "role": getattr(message, "type", message.__class__.__name__),
                "content": self._chunk_text(getattr(message, "content", "")),
            }
            for message in rendered_messages
        ]
        return json.dumps(serialized_messages, ensure_ascii=False)
