from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from router_service.core.shared.domain import AgentStreamChunk, Task, TaskStatus

MISSING = object()


class AgentClient(Protocol):
    """Protocol for downstream intent agents that support streaming and cancellation."""

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        """Stream normalized agent chunks for one task."""
        ...

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        """Cancel a previously started task on the downstream agent."""
        ...

    async def close(self) -> None:
        """Release any transport resources held by the agent client."""
        ...


class RequestPayloadBuilder:
    """Build downstream agent request payloads from router task state."""

    def build(self, task: Task, user_input: str) -> dict[str, Any]:
        """Build the downstream agent request payload from task context and mappings."""
        if not task.field_mapping:
            payload = self._default_payload(task, user_input)
        else:
            payload: dict[str, Any] = {}
            slots_data: dict[str, Any] = {}
            config_variables: list[dict[str, str]] = []

            for target_path, source_path in task.field_mapping.items():
                value = self._resolve_source(source_path, task, user_input)
                if value is MISSING:
                    continue

                if target_path.startswith("config_variables.slots_data."):
                    slot_key = target_path.removeprefix("config_variables.slots_data.")
                    slots_data[slot_key] = value
                elif target_path.startswith("config_variables."):
                    var_name = target_path.removeprefix("config_variables.")
                    str_value = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)
                    config_variables.append({"name": var_name, "value": str_value})
                else:
                    self._set_nested_value(payload, target_path, value)

            if slots_data:
                config_variables.append({
                    "name": "slots_data",
                    "value": json.dumps(slots_data, ensure_ascii=False),
                })
            if config_variables:
                payload["config_variables"] = config_variables

        self._validate_required_fields(payload, task.request_schema)
        return payload

    def _default_payload(self, task: Task, user_input: str) -> dict[str, Any]:
        """Build the config-variables payload shape used when no explicit field mapping exists."""
        config_variables: list[dict[str, str]] = [
            {"name": "custID", "value": str(task.input_context.get("cust_id", ""))},
            {"name": "sessionID", "value": task.session_id},
            {"name": "currentDisplay", "value": ""},
            {"name": "agentSessionID", "value": task.session_id},
        ]
        if task.slot_memory:
            config_variables.append({
                "name": "slots_data",
                "value": json.dumps(dict(task.slot_memory), ensure_ascii=False),
            })
        return {
            "session_id": task.session_id,
            "txt": user_input,
            "stream": True,
            "config_variables": config_variables,
        }

    def _validate_required_fields(self, payload: dict[str, Any], request_schema: dict[str, Any]) -> None:
        """Validate required request fields declared by the intent schema."""
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
        """Resolve one field-mapping expression against task, session, and slot sources."""
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
        """Assign a value into a nested dict using dot-separated target paths."""
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
        """Read a value from a nested dict using dot-separated lookup paths."""
        parts = [part for part in dotted_path.split(".") if part]
        current = source
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return MISSING
            current = current[part]
        return current


class StreamingAgentClient:
    """HTTP-based downstream agent client that normalizes streaming responses."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        http_timeout_seconds: float = 60.0,
    ) -> None:
        """Initialize the streaming client and optionally own the HTTP client lifecycle."""
        self.payload_builder = RequestPayloadBuilder()
        self.http_timeout_seconds = http_timeout_seconds
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(http_timeout_seconds),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )

    async def stream(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        """Stream normalized chunks from the configured downstream agent."""
        if task.agent_url.startswith(("http://", "https://")):
            async for chunk in self._stream_via_http(task, user_input):
                yield chunk
            return

        yield self._failure_chunk(task, f"Unsupported agent_url scheme: {task.agent_url}")

    async def _stream_via_http(self, task: Task, user_input: str) -> AsyncIterator[AgentStreamChunk]:
        """Call the downstream agent over HTTP and normalize its response protocol."""
        try:
            payload = self.payload_builder.build(task, user_input)
        except ValueError as exc:
            yield self._failure_chunk(task, str(exc))
            return

        emitted_chunk = False
        try:
            async with self.http_client.stream(
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

        if not emitted_chunk:
            yield self._failure_chunk(task, "Agent returned no stream events")

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        """Call the downstream agent cancellation endpoint."""
        if agent_url is None:
            raise RuntimeError("agent_url is required for agent cancellation")
        if not agent_url.startswith(("http://", "https://")):
            raise RuntimeError(f"Unsupported agent_url scheme: {agent_url}")

        cancel_url = self._cancel_url(agent_url)
        response = await self.http_client.post(
            cancel_url,
            json={"sessionId": session_id, "taskId": task_id},
        )
        response.raise_for_status()

    async def close(self) -> None:
        """Close the owned HTTP client when this instance created it."""
        if self._owns_http_client:
            await self.http_client.aclose()

    def _data_text_to_chunks(self, task: Task, text: str) -> list[AgentStreamChunk]:
        """Parse one textual stream frame into normalized agent chunks."""
        if not text or text == "[DONE]":
            return []
        parsed = json.loads(text)
        return self._payloads_to_chunks(task, parsed)

    def _payloads_to_chunks(self, task: Task, payload: Any) -> list[AgentStreamChunk]:
        """Normalize list, envelope, or single-event payloads into chunk objects."""
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
        """Normalize one downstream payload dict into an `AgentStreamChunk`."""
        # Handle nested format: additional_kwargs.node_output.output
        nested_output = self._extract_nested_output(payload)
        if nested_output:
            payload = nested_output

        slot_memory = payload.get("slot_memory")
        if isinstance(slot_memory, dict):
            task.slot_memory.update(slot_memory)

        chunk_payload = payload.get("payload")
        normalized_payload = dict(chunk_payload) if isinstance(chunk_payload, dict) else {}
        if isinstance(slot_memory, dict):
            normalized_payload.setdefault("slot_memory", dict(task.slot_memory))

        # Support both ishandover and isHandOver
        ishandover = payload.get("ishandover") or payload.get("isHandOver")
        # Support handOverReason for logging/debugging
        hand_over_reason = payload.get("handOverReason")
        status = self._resolve_status(payload.get("status"), ishandover)
        if not isinstance(ishandover, bool):
            ishandover = status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
        if status == TaskStatus.WAITING_USER_INPUT:
            ishandover = False

        # Extract content from payload or data array
        content = self._extract_content(payload)

        return AgentStreamChunk(
            task_id=task.task_id,
            event=str(payload.get("event") or ("final" if ishandover else "message")),
            content=content,
            ishandover=ishandover,
            status=status,
            payload=normalized_payload,
        )

    def _extract_nested_output(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Extract output from nested additional_kwargs.node_output.output format."""
        additional_kwargs = payload.get("additional_kwargs")
        if not isinstance(additional_kwargs, dict):
            return None

        node_output = additional_kwargs.get("node_output")
        if not isinstance(node_output, dict):
            return None

        output = node_output.get("output")
        if not isinstance(output, str):
            return None

        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return None

    def _extract_content(self, payload: dict[str, Any]) -> str:
        """Extract content from payload, supporting data array format."""
        content = payload.get("content") or payload.get("message") or ""
        if content:
            return str(content)

        # Try to extract from data array (old format)
        data = payload.get("data")
        if isinstance(data, list) and data:
            first_item = data[0] if isinstance(data[0], dict) else {}
            answer = first_item.get("answer", "")
            if answer:
                return str(answer)

        return ""

    def _resolve_status(self, raw_status: Any, ishandover: Any) -> TaskStatus:
        """Resolve agent-provided status fields into the canonical task status enum."""
        if isinstance(raw_status, TaskStatus):
            return raw_status
        if isinstance(raw_status, str):
            normalized = raw_status.strip().lower()
            if normalized in TaskStatus._value2member_map_:
                return TaskStatus(normalized)
        if ishandover is False:
            return TaskStatus.WAITING_USER_INPUT
        return TaskStatus.COMPLETED

    def _failure_chunk(self, task: Task, message: str) -> AgentStreamChunk:
        """Build a terminal failure chunk for transport or parsing errors."""
        return AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=message,
            ishandover=True,
            status=TaskStatus.FAILED,
        )

    def _cancel_url(self, agent_url: str) -> str:
        """Derive the agent cancellation URL from the agent run URL."""
        if agent_url.endswith("/run"):
            return agent_url[:-4] + "/cancel"
        if agent_url.endswith("/run/"):
            return agent_url[:-5] + "/cancel"
        return agent_url.rstrip("/") + "/cancel"
