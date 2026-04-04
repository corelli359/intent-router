from __future__ import annotations

import json
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any, Awaitable, Callable, Literal, Protocol

import httpx
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, model_validator

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


class JsonLLMClient(Protocol):
    async def run_json(
        self,
        *,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> Any: ...


@dataclass(slots=True)
class LangChainLLMClient:
    base_url: str
    default_model: str
    api_key: str | None = None
    timeout_seconds: float = 30.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    structured_output_method: Literal["function_calling", "json_mode", "json_schema"] = "json_mode"
    http_async_client: httpx.AsyncClient | None = None

    async def run_json(
        self,
        *,
        prompt: ChatPromptTemplate,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> Any:
        response_text = await self._stream_prompt(prompt, variables, model=model, on_delta=on_delta)
        return extract_json_value(response_text)

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
