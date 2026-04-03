from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from router_core.domain import AgentStreamChunk, Task, TaskStatus
from router_core.llm_client import LLMClient


ORDER_RE = re.compile(r"\b(\d{3,})\b")
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
MISSING = object()


class AgentClient(Protocol):
    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]: ...


class MockStreamingAgentClient:
    """Internal streaming agent simulator used before real agent URLs are wired in."""

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        intent = task.intent_code
        if intent == "query_order_status":
            yield self._handle_order(task, user_input)
            return
        if intent == "cancel_appointment":
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="明天的预约已取消",
                ishandover=True,
                status=TaskStatus.COMPLETED,
            )
            return
        if intent == "update_shipping_address":
            yield self._handle_address(task, user_input)
            return
        if intent == "transfer_money":
            async for chunk in self._handle_transfer(task, user_input):
                yield chunk
            return
        if intent == "pay_bill":
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="缴费任务已创建，待接入真实缴费 Agent",
                ishandover=True,
                status=TaskStatus.COMPLETED,
            )
            return
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"{intent} 暂无模拟实现",
            ishandover=True,
            status=TaskStatus.FAILED,
        )

    def _handle_order(self, task: Task, user_input: str) -> AgentStreamChunk:
        match = ORDER_RE.search(user_input)
        if match is None:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请提供订单号",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
        order_id = match.group(1)
        task.slot_memory["order_id"] = order_id
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"订单 {order_id} 当前状态为已发货",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"order_id": order_id},
        )

    def _handle_address(self, task: Task, user_input: str) -> AgentStreamChunk:
        if "路" not in user_input and "区" not in user_input and "号" not in user_input:
            return AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请提供新的收货地址",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
        task.slot_memory["address"] = user_input
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content="地址已更新完成",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"address": user_input},
        )

    async def _handle_transfer(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        if "张三" in user_input or "李四" in user_input:
            task.slot_memory.setdefault("payee", "张三" if "张三" in user_input else "李四")
        amount_match = AMOUNT_RE.search(user_input)
        if amount_match:
            task.slot_memory["amount"] = amount_match.group(1)
        if "工资卡" in user_input or "储蓄卡" in user_input:
            task.slot_memory["funding_account"] = user_input

        if "amount" not in task.slot_memory:
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请提供转账金额",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
            return
        if "funding_account" not in task.slot_memory:
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="message",
                content="请确认付款账户",
                ishandover=False,
                status=TaskStatus.WAITING_USER_INPUT,
            )
            return
        amount = task.slot_memory["amount"]
        payee = task.slot_memory.get("payee", "收款人")
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已完成向 {payee} 转账 {amount} 元",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=dict(task.slot_memory),
        )


class RequestPayloadBuilder:
    def build(self, task: Task, user_input: str) -> dict[str, Any]:
        if not task.field_mapping:
            payload = self._default_payload(task, user_input)
        else:
            payload: dict[str, Any] = {}
            for target_path, source_path in task.field_mapping.items():
                value = self._resolve_source(source_path, task, user_input)
                if value is MISSING:
                    continue
                self._set_nested_value(payload, target_path, value)

        self._validate_required_fields(payload, task.request_schema)
        return payload

    def _default_payload(self, task: Task, user_input: str) -> dict[str, Any]:
        return {
            "sessionId": task.session_id,
            "taskId": task.task_id,
            "intentCode": task.intent_code,
            "input": user_input,
            "context": {
                "recentMessages": task.input_context.get("recent_messages", []),
                "longTermMemory": task.input_context.get("long_term_memory", []),
            },
            "slots": dict(task.slot_memory),
        }

    def _validate_required_fields(self, payload: dict[str, Any], request_schema: dict[str, Any]) -> None:
        required_fields = request_schema.get("required", [])
        if not isinstance(required_fields, list):
            return
        missing = [
            field
            for field in required_fields
            if self._get_nested_value(payload, str(field)) is MISSING
        ]
        if missing:
            raise ValueError(f"Missing required agent request fields: {', '.join(missing)}")

    def _resolve_source(self, expression: str, task: Task, user_input: str) -> Any:
        if not expression.startswith("$"):
            return expression

        recent_messages = task.input_context.get("recent_messages", [])
        long_term_memory = task.input_context.get("long_term_memory", [])
        sources: dict[str, Any] = {
            "session": {
                "id": task.session_id,
                "cust_id": task.input_context.get("cust_id"),
            },
            "task": {
                "id": task.task_id,
                "status": task.status.value,
            },
            "intent": {
                "code": task.intent_code,
                "name": task.intent_name,
                "description": task.intent_description,
            },
            "message": {
                "current": user_input,
            },
            "context": {
                "recent_15_messages": recent_messages,
                "recent_messages": recent_messages,
                "long_term_memory": long_term_memory,
            },
            "memory": {
                "long_term": long_term_memory,
            },
            "entities": task.slot_memory,
            "slots": task.slot_memory,
            "slot_memory": task.slot_memory,
        }

        path = expression.removeprefix("$")
        return self._get_nested_value(sources, path)

    def _set_nested_value(self, target: dict[str, Any], dotted_path: str, value: Any) -> None:
        parts = [part for part in dotted_path.split(".") if part]
        if not parts:
            return
        cursor = target
        for part in parts[:-1]:
            next_value = cursor.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                cursor[part] = next_value
            cursor = next_value
        cursor[parts[-1]] = value

    def _get_nested_value(self, source: Any, dotted_path: str) -> Any:
        parts = [part for part in dotted_path.split(".") if part]
        current = source
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return MISSING
            current = current[part]
        return current


class StreamingAgentClient:
    def __init__(
        self,
        *,
        llm_client: LLMClient | None = None,
        llm_model: str | None = None,
        enable_llm_for_mock_scheme: bool = False,
        http_client: httpx.AsyncClient | None = None,
        http_timeout_seconds: float = 60.0,
    ) -> None:
        self.mock_client = MockStreamingAgentClient()
        self.payload_builder = RequestPayloadBuilder()
        self.llm_client = llm_client
        self.llm_model = llm_model
        self.enable_llm_for_mock_scheme = enable_llm_for_mock_scheme
        self.http_client = http_client
        self.http_timeout_seconds = http_timeout_seconds

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        if task.agent_url.startswith("mock://") and not self.enable_llm_for_mock_scheme:
            async for chunk in self.mock_client.stream(task, user_input):
                yield chunk
            return

        if task.agent_url.startswith("llm://") or (
            task.agent_url.startswith("mock://") and self.enable_llm_for_mock_scheme
        ):
            async for chunk in self._stream_via_llm(task, user_input):
                yield chunk
            return

        if task.agent_url.startswith(("http://", "https://")):
            async for chunk in self._stream_via_http(task, user_input):
                yield chunk
            return

        yield self._failure_chunk(task, f"Unsupported agent_url scheme: {task.agent_url}")

    async def _stream_via_llm(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        if self.llm_client is None:
            yield self._failure_chunk(task, "LLM agent is enabled but ROUTER_LLM_* is not fully configured")
            return

        model = self._resolve_llm_model(task.agent_url)
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def publish_delta(delta: str) -> None:
            if not delta:
                return
            await queue.put(("delta", delta))

        async def runner() -> None:
            try:
                response = await self.llm_client.run_agent(
                    task=task,
                    user_input=user_input,
                    model=model or self.llm_model,
                    on_delta=publish_delta,
                )
            except Exception as exc:
                await queue.put(("error", exc))
                return
            await queue.put(("final", response))

        runner_task = asyncio.create_task(runner())
        try:
            while True:
                item_type, value = await queue.get()
                if item_type == "delta":
                    yield AgentStreamChunk(
                        task_id=task.task_id,
                        event="message",
                        content=str(value),
                        ishandover=False,
                        status=TaskStatus.RUNNING,
                    )
                    continue
                if item_type == "error":
                    yield self._failure_chunk(task, f"LLM agent call failed: {value}")
                    return
                if item_type == "final":
                    yield self._payload_to_chunk(task, value.model_dump())
                    return
        finally:
            await runner_task

    async def _stream_via_http(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        try:
            payload = self.payload_builder.build(task, user_input)
        except ValueError as exc:
            yield self._failure_chunk(task, str(exc))
            return

        client = self.http_client
        should_close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.http_timeout_seconds)
            should_close_client = True

        emitted_chunk = False
        try:
            async with client.stream(
                "POST",
                task.agent_url,
                json=payload,
                headers={"Accept": "text/event-stream, application/x-ndjson, application/json"},
            ) as response:
                if response.status_code >= 400:
                    yield self._failure_chunk(
                        task,
                        f"Agent HTTP request failed with status {response.status_code}: {await response.aread()}",
                    )
                    return

                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type and "stream" not in content_type:
                    raw_body = await response.aread()
                    parsed = json.loads(raw_body.decode("utf-8"))
                    for chunk in self._payloads_to_chunks(task, parsed):
                        emitted_chunk = True
                        yield chunk
                    return

                sse_buffer: list[str] = []
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        if sse_buffer:
                            for chunk in self._data_text_to_chunks(task, "\n".join(sse_buffer)):
                                emitted_chunk = True
                                yield chunk
                            sse_buffer = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        sse_buffer.append(line.removeprefix("data:").lstrip())
                        continue
                    if "text/event-stream" in content_type:
                        continue
                    for chunk in self._data_text_to_chunks(task, line):
                        emitted_chunk = True
                        yield chunk

                if sse_buffer:
                    for chunk in self._data_text_to_chunks(task, "\n".join(sse_buffer)):
                        emitted_chunk = True
                        yield chunk
        except Exception as exc:
            yield self._failure_chunk(task, f"Agent HTTP request failed: {exc}")
            return
        finally:
            if should_close_client:
                await client.aclose()

        if not emitted_chunk:
            yield self._failure_chunk(task, "Agent returned no stream events")

    def _data_text_to_chunks(self, task: Task, text: str) -> list[AgentStreamChunk]:
        if not text or text == "[DONE]":
            return []
        parsed = json.loads(text)
        return self._payloads_to_chunks(task, parsed)

    def _payloads_to_chunks(self, task: Task, payload: Any) -> list[AgentStreamChunk]:
        if isinstance(payload, list):
            return [self._payload_to_chunk(task, item) for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            return [
                self._payload_to_chunk(task, item)
                for item in payload["events"]
                if isinstance(item, dict)
            ]
        if isinstance(payload, dict):
            return [self._payload_to_chunk(task, payload)]
        return [self._failure_chunk(task, f"Unsupported agent response payload: {payload!r}")]

    def _payload_to_chunk(self, task: Task, payload: dict[str, Any]) -> AgentStreamChunk:
        slot_memory = payload.get("slot_memory")
        if isinstance(slot_memory, dict):
            task.slot_memory.update(slot_memory)

        chunk_payload = payload.get("payload")
        normalized_payload = dict(chunk_payload) if isinstance(chunk_payload, dict) else {}
        if isinstance(slot_memory, dict):
            normalized_payload.setdefault("slot_memory", dict(task.slot_memory))

        ishandover = payload.get("ishandover")
        status = self._resolve_status(payload.get("status"), ishandover)
        if not isinstance(ishandover, bool):
            ishandover = status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
        if status == TaskStatus.WAITING_USER_INPUT:
            ishandover = False

        return AgentStreamChunk(
            task_id=task.task_id,
            event=str(payload.get("event") or ("final" if ishandover else "message")),
            content=str(payload.get("content") or payload.get("message") or ""),
            ishandover=ishandover,
            status=status,
            payload=normalized_payload,
        )

    def _resolve_status(self, raw_status: Any, ishandover: Any) -> TaskStatus:
        if isinstance(raw_status, TaskStatus):
            return raw_status
        if isinstance(raw_status, str):
            normalized = raw_status.strip().lower()
            if normalized in TaskStatus._value2member_map_:
                return TaskStatus(normalized)
        if ishandover is False:
            return TaskStatus.WAITING_USER_INPUT
        return TaskStatus.COMPLETED

    def _resolve_llm_model(self, agent_url: str) -> str | None:
        if not agent_url.startswith("llm://"):
            return None
        model = agent_url.removeprefix("llm://").strip().strip("/")
        if not model or model == "default":
            return None
        return model

    def _failure_chunk(self, task: Task, message: str) -> AgentStreamChunk:
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=message,
            ishandover=True,
            status=TaskStatus.FAILED,
        )
