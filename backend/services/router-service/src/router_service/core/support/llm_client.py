from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Awaitable, Callable, Literal, Protocol
from uuid import uuid4

import httpx
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field, model_validator
from router_service.core.support.json_codec import (
    JSONDecodeError,
    extract_first_json_value,
    json_dumpb,
    json_dumps,
    json_loads,
)
from router_service.core.support.jwt_utils import AuthHTTPClient
from router_service.core.support.trace_logging import current_trace_id

AsyncDeltaCallback = Callable[[str], Awaitable[None]]
logger = logging.getLogger(__name__)


class LLMHTTPStatusError(RuntimeError):
    """HTTP-layer LLM failure with provider status metadata for retry logic."""

    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self.body = body
        message = f"LLM HTTP request failed with status {status_code}"
        if body is not None:
            message = f"{message}; body={body}"
        super().__init__(message)


class LLMServiceUnavailableError(RuntimeError):
    """Semantic wrapper used when router business flow must surface LLM unavailability."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.details = dict(details or {})
        super().__init__(message)


def extract_json_value(raw_text: str) -> Any:
    """Extract the first valid JSON object or array from raw LLM output text."""
    return extract_first_json_value(raw_text)


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

    async def aclose(self) -> None:
        """Release any network or runtime resources held by the client."""
        ...


def llm_exception_is_retryable(exc: Exception) -> bool:
    """Return whether an exception should be retried as a transient LLM failure."""
    return getattr(exc, "status_code", None) == 429


def llm_exception_details(exc: Exception) -> dict[str, Any]:
    """Extract stable debugging details from one LLM exception."""
    details: dict[str, Any] = {
        "error_type": type(exc).__name__,
    }
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        details["status_code"] = status_code
    body = getattr(exc, "body", None)
    if body is not None:
        details["body"] = body
    error_message = str(exc).strip()
    if error_message:
        details["error"] = error_message
    return details


@dataclass(slots=True)
class LangChainLLMClient:
    """OpenAI-compatible JSON LLM client with rate-limit aware retry handling."""

    base_url: str
    default_model: str
    api_key: str | None = None
    temperature: float = 0.0
    timeout_seconds: float = 30.0
    rate_limit_max_retries: int = 2
    rate_limit_retry_delay_seconds: float = 2.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = "json_mode"
    http_async_client: httpx.AsyncClient | None = None

    def __post_init__(self) -> None:
        """Create a reusable async HTTP pool when the caller did not provide one."""
        if self.http_async_client is None:
            self.http_async_client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=256, keepalive_expiry=30.0),
            )

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
        variable_keys = ",".join(sorted(str(key) for key in variables.keys()))
        started_at = time.perf_counter()
        if logger.isEnabledFor(logging.DEBUG):
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
        rendered_messages = self._render_chat_messages(prompt, variables)
        request = self._request_payload(
            messages=rendered_messages,
            model=model,
            stream=True,
        )
        chunks: list[str] = []
        assert self.http_async_client is not None
        async with self.http_async_client.stream(
            "POST",
            self._chat_completions_url(),
            headers=self._request_headers(),
            content=json_dumpb(request),
        ) as response:
            await self._raise_for_status(response)
            async for line in response.aiter_lines():
                raw_line = line.strip()
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                data = raw_line[5:].strip()
                if data == "[DONE]":
                    break
                chunk_payload = json_loads(data)
                text = self._chunk_text(self._stream_chunk_content(chunk_payload))
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
        rendered_messages = self._render_chat_messages(prompt, variables)
        request = self._request_payload(
            messages=rendered_messages,
            model=model,
            stream=False,
        )
        assert self.http_async_client is not None
        response = await self.http_async_client.post(
            self._chat_completions_url(),
            headers=self._request_headers(),
            content=json_dumpb(request),
        )
        await self._raise_for_status(response)
        payload = json_loads(response.content)
        return self._chunk_text(self._completion_content(payload))

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

    def _chat_completions_url(self) -> str:
        """Return the full OpenAI-compatible chat completions endpoint."""
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def _request_headers(self) -> dict[str, str]:
        """Build outbound HTTP headers for OpenAI-compatible requests."""
        headers = {
            "content-type": "application/json",
            **self.extra_headers,
        }
        if self.api_key and not isinstance(self.http_async_client, AuthHTTPClient):
            headers.setdefault("Authorization", f"Bearer {self.api_key}")
        return headers

    def _request_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str | None,
        stream: bool,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible request payload."""
        return {
            "model": model or self.default_model,
            "messages": messages,
            "stream": stream,
            "temperature": self.temperature,
        }

    def _render_chat_messages(
        self,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Render one prompt template into OpenAI-compatible chat messages."""
        rendered_messages = prompt.format_messages(**variables)
        return [
            {
                "role": self._message_role(message),
                "content": self._message_content(getattr(message, "content", "")),
            }
            for message in rendered_messages
        ]

    def _message_role(self, message: Any) -> str:
        """Normalize LangChain message roles to OpenAI-compatible roles."""
        role = str(getattr(message, "type", message.__class__.__name__)).lower()
        if role in {"human", "user"}:
            return "user"
        if role in {"ai", "assistant"}:
            return "assistant"
        if role == "system":
            return "system"
        if role == "tool":
            return "tool"
        return role

    def _message_content(self, content: Any) -> Any:
        """Preserve request content shapes accepted by OpenAI-compatible chat APIs."""
        if isinstance(content, (str, list)):
            return content
        return str(content or "")

    async def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise one retry-aware status error for non-2xx provider responses."""
        if not response.is_error:
            return
        body_bytes = await response.aread()
        try:
            body = json_loads(body_bytes)
        except Exception:
            body = body_bytes.decode("utf-8", errors="replace")
        raise LLMHTTPStatusError(response.status_code, body=body)

    def _completion_content(self, payload: Any) -> Any:
        """Extract assistant content from one non-streaming OpenAI-compatible response."""
        if not isinstance(payload, dict):
            raise ValueError("LLM response payload must be a JSON object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("LLM response payload does not contain choices")
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("LLM response payload does not contain message content")
        return message.get("content", "")

    def _stream_chunk_content(self, payload: Any) -> Any:
        """Extract assistant delta content from one streaming OpenAI-compatible chunk."""
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first_choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            return ""
        return delta.get("content", "")

    def _render_prompt_messages(self, prompt: ChatPromptTemplate, variables: dict[str, Any]) -> str:
        """Render the final prompt messages into a JSON log payload."""
        try:
            rendered_messages = self._render_chat_messages(prompt, variables)
        except Exception as exc:
            return json_dumps(
                [{"role": "render_error", "content": f"prompt render failed: {exc}"}],
            )
        return json_dumps(rendered_messages)
