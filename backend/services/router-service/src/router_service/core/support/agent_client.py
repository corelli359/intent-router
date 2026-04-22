from __future__ import annotations
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from router_service.core.shared.domain import AgentStreamChunk, Task, TaskStatus
from router_service.core.support.json_codec import JSONDecodeError, json_dumps, json_loads

MISSING = object()


class AgentPayloadParseError(ValueError):
    """Raised when the downstream agent returns an invalid JSON payload."""


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
            uses_config_variables = False
        else:
            payload: dict[str, Any] = {}
            config_variables: dict[str, Any] = {}
            uses_config_variables = False
            slots_data_requested = False
            for target_path, source_path in task.field_mapping.items():
                if target_path == "config_variables.slots_data" or target_path.startswith(
                    "config_variables.slots_data."
                ):
                    slots_data_requested = True
                value = self._resolve_source(source_path, task, user_input)
                if value is MISSING:
                    continue
                if self._set_config_variable_value(config_variables, target_path, value):
                    uses_config_variables = True
                    continue
                self._set_nested_value(payload, target_path, value)
            if uses_config_variables:
                if slots_data_requested:
                    config_variables.setdefault("slots_data", {})
                payload["config_variables"] = [
                    {"name": name, "value": self._serialize_config_variable_value(value)}
                    for name, value in config_variables.items()
                ]
                payload.setdefault("stream", True)

        if not uses_config_variables:
            payload.setdefault(
                "intent",
                {
                    "code": task.intent_code,
                    "name": task.intent_name,
                    "description": task.intent_description,
                    "examples": list(task.intent_examples),
                },
            )

        self._validate_required_fields(payload, task.request_schema)
        return payload

    def _default_payload(self, task: Task, user_input: str) -> dict[str, Any]:
        """Build the default payload shape used when no explicit field mapping exists."""
        return {
            "sessionId": task.session_id,
            "taskId": task.task_id,
            "intentCode": task.intent_code,
            "input": user_input,
            "intent": {
                "code": task.intent_code,
                "name": task.intent_name,
                "description": task.intent_description,
                "examples": list(task.intent_examples),
            },
            "context": {
                "recentMessages": task.input_context.get("recent_messages", []),
                "longTermMemory": task.input_context.get("long_term_memory", []),
            },
            "slots": dict(task.slot_memory),
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
            return self._coerce_literal_value(expression)

        recent_messages = task.input_context.get("recent_messages", [])
        long_term_memory = task.input_context.get("long_term_memory", [])
        config_variables = task.input_context.get("config_variables", {})
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
                "examples": list(task.intent_examples),
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
            "config_variables": config_variables if isinstance(config_variables, dict) else {},
            "entities": task.slot_memory,
            "slots": task.slot_memory,
            "slot_memory": task.slot_memory,
        }

        path = expression.removeprefix("$")
        return self._get_nested_value(sources, path)

    def _coerce_literal_value(self, expression: str) -> Any:
        """Normalize literal mapping values for well-known JSON scalars."""
        normalized = expression.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        if normalized == "null":
            return None
        return expression

    def _set_config_variable_value(
        self,
        config_variables: dict[str, Any],
        target_path: str,
        value: Any,
    ) -> bool:
        """Assign one mapping into the config_variables envelope."""
        prefix = "config_variables."
        if not target_path.startswith(prefix):
            return False

        variable_path = target_path.removeprefix(prefix)
        if not variable_path:
            return True
        if variable_path == "slots_data":
            config_variables["slots_data"] = dict(value) if isinstance(value, dict) else value
            return True
        if variable_path.startswith("slots_data."):
            slots_data = config_variables.get("slots_data")
            if not isinstance(slots_data, dict):
                slots_data = {}
                config_variables["slots_data"] = slots_data
            self._set_nested_value(slots_data, variable_path.removeprefix("slots_data."), value)
            return True

        config_variables[variable_path] = value
        return True

    def _serialize_config_variable_value(self, value: Any) -> str:
        """Serialize config variable values to the downstream string-only contract."""
        if isinstance(value, str):
            return value
        if value is None or isinstance(value, (dict, list, bool, int, float)):
            return json_dumps(value)
        return str(value)

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
                    parsed = self._parse_json_payload(raw_body, is_stream=False)
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
        except AgentPayloadParseError as exc:
            yield self._failure_chunk(task, str(exc))
            return
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
        parsed = self._parse_json_payload(text, is_stream=True)
        return self._payloads_to_chunks(task, parsed)

    def _parse_json_payload(self, value: str | bytes, *, is_stream: bool) -> Any:
        """Parse a downstream JSON payload and raise a readable contract error on failure."""
        try:
            return json_loads(value)
        except JSONDecodeError as exc:
            raw_text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            preview = raw_text.strip()
            if not preview:
                detail = "Agent returned empty stream payload" if is_stream else "Agent returned empty response payload"
            else:
                label = "stream payload" if is_stream else "response payload"
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                detail = f"Agent returned invalid JSON {label}: {preview}"
            raise AgentPayloadParseError(detail) from exc

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
        payload = self._normalized_agent_payload(payload)
        slot_memory = payload.get("slot_memory")
        if isinstance(slot_memory, dict):
            task.slot_memory.update(slot_memory)

        chunk_payload = payload.get("payload")
        normalized_payload = dict(chunk_payload) if isinstance(chunk_payload, dict) else {}
        if isinstance(slot_memory, dict):
            normalized_payload.setdefault("slot_memory", dict(task.slot_memory))

        normalized_output: dict[str, Any] = {}
        if isinstance(chunk_payload, dict):
            normalized_output.update(chunk_payload)
        if isinstance(slot_memory, dict):
            normalized_output["slot_memory"] = dict(task.slot_memory)
        for key in (
            "node_id",
            "isHandOver",
            "handOverReason",
            "data",
            "status",
            "event",
            "completion_state",
            "completion_reason",
        ):
            value = payload.get(key)
            if value is not None:
                normalized_output[key] = value
        resolved_node_id = self._resolved_node_id(payload)
        if resolved_node_id is not None:
            normalized_output.setdefault("node_id", resolved_node_id)
        normalized_output.setdefault("intent_code", task.intent_code)

        ishandover = payload.get("ishandover")
        if not isinstance(ishandover, bool):
            ishandover = payload.get("isHandOver")
        status = self._resolve_status(payload.get("status"), ishandover)
        if not isinstance(ishandover, bool):
            ishandover = status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
        if status == TaskStatus.WAITING_USER_INPUT:
            ishandover = False

        return AgentStreamChunk(
            task_id=task.task_id,
            event=str(payload.get("event") or ("final" if ishandover else "message")),
            content=self._payload_content(payload),
            ishandover=ishandover,
            status=status,
            payload=normalized_payload,
            output=normalized_output,
        )

    def _normalized_agent_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Flatten legacy nested output wrappers into the primary payload dict."""
        nested_output = self._get_nested_payload_value(payload, "additional_kwargs.node_output.output")
        if isinstance(nested_output, str):
            try:
                nested_output = json_loads(nested_output)
            except JSONDecodeError:
                return payload
        if not isinstance(nested_output, dict):
            return payload
        normalized = dict(payload)
        normalized.update(nested_output)
        return normalized

    def _resolved_node_id(self, payload: dict[str, Any]) -> str | None:
        """Resolve the agent node id from either flat or legacy nested payloads."""
        direct_value = payload.get("node_id")
        if direct_value not in (None, ""):
            return str(direct_value)
        nested_value = self._get_nested_payload_value(payload, "additional_kwargs.node_id")
        if nested_value not in (None, ""):
            return str(nested_value)
        return None

    def _payload_content(self, payload: dict[str, Any]) -> str:
        """Resolve the human-facing content field across old and new agent payloads."""
        for key in ("content", "message"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)

        data_items = payload.get("data")
        if isinstance(data_items, list):
            for item in data_items:
                if not isinstance(item, dict):
                    continue
                answer = item.get("answer")
                if answer not in (None, ""):
                    return str(answer)
        return ""

    def _get_nested_payload_value(self, source: Any, dotted_path: str) -> Any:
        """Read one nested payload value from a dict using dot-separated lookup paths."""
        parts = [part for part in dotted_path.split(".") if part]
        current = source
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return MISSING
            current = current[part]
        return current

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
