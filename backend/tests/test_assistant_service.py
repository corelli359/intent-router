from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI
from starlette.responses import StreamingResponse


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ASSISTANT_SRC = BACKEND_ROOT / "services" / "assistant-service" / "src"
if str(ASSISTANT_SRC) not in sys.path:
    sys.path.insert(0, str(ASSISTANT_SRC))

from assistant_service.app import (  # noqa: E402
    AssistantRunRequest,
    AssistantTaskCompletionRequest,
    RouterForwardService,
    create_app,
    get_router_forward_service,
)
from tests.test_router_api_v2 import (  # noqa: E402
    _AssistantProtocolTransferAgentClient,
    _ContractTransferUnderstandingValidator,
    _TransferOnlyRecognizer,
    _assistant_protocol_ag_trans_intent,
    _test_v2_app,
)


def _parse_sse_frames(raw_text: str) -> list[tuple[str, str]]:
    frames: list[tuple[str, str]] = []
    for chunk in raw_text.split("\n\n"):
        event_name: str | None = None
        data_value: str | None = None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_value = line.split(":", 1)[1].strip()
        if event_name is not None and data_value is not None:
            frames.append((event_name, data_value))
    return frames


def _message_payloads(raw_text: str) -> list[dict[str, object]]:
    return [
        json.loads(data)
        for event, data in _parse_sse_frames(raw_text)
        if event == "message"
    ]


def _is_recognition_payload(payload: dict[str, object]) -> bool:
    output = payload.get("output")
    return isinstance(output, dict) and output.get("stage") == "intent_recognition"


def _non_recognition_payloads(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    return [payload for payload in payloads if not _is_recognition_payload(payload)]


def test_assistant_service_forwards_non_stream_request_to_router() -> None:
    async def run() -> None:
        router_app = FastAPI()
        received: dict[str, object] = {}

        @router_app.post("/api/v1/message")
        async def router_message(payload: dict[str, object]) -> dict[str, object]:
            received["payload"] = payload
            return {
                "ok": True,
                "current_task": "task_001",
                "task_list": [{"name": "task_001", "status": "completed"}],
                "status": "completed",
                "intent_code": "AG_TRANS",
                "completion_state": 2,
                "completion_reason": "agent_final_done",
                "slot_memory": {"payee_name": "小明", "amount": "200"},
                "message": "执行图已完成",
                "output": {
                    "isHandOver": True,
                    "data": [{"answer": "||200|小明|"}],
                },
            }

        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            response = await client.post(
                "/api/assistant/run",
                json=AssistantRunRequest(
                    sessionId="session_assistant_001",
                    txt="给小明转账 200",
                    config_variables=[
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "session_assistant_001"},
                    ],
                ).model_dump(mode="json", by_alias=True),
            )

        await router_client.aclose()

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "current_task": "task_001",
            "task_list": [{"name": "task_001", "status": "completed"}],
            "status": "completed",
            "intent_code": "AG_TRANS",
            "completion_state": 2,
            "completion_reason": "agent_final_done",
            "slot_memory": {"payee_name": "小明", "amount": "200"},
            "message": "执行图已完成",
            "output": {
                "isHandOver": True,
                "data": [{"answer": "||200|小明|"}],
            },
        }
        assert received == {
            "payload": {
                "sessionId": "session_assistant_001",
                "txt": "给小明转账 200",
                "config_variables": [
                    {"name": "custID", "value": "C0001"},
                    {"name": "sessionID", "value": "session_assistant_001"},
                ],
                "executionMode": "execute",
                "stream": False,
            },
        }

    asyncio.run(run())


def test_assistant_service_forwards_non_stream_task_completion_to_router() -> None:
    async def run() -> None:
        router_app = FastAPI()
        received: dict[str, object] = {}

        @router_app.post("/api/v1/task/completion")
        async def router_task_completion(payload: dict[str, object]) -> dict[str, object]:
            received["payload"] = payload
            return {
                "ok": True,
                "current_task": "task_001",
                "task_list": [{"name": "task_001", "status": "completed"}],
                "status": "completed",
                "intent_code": "AG_TRANS",
                "completion_state": 2,
                "completion_reason": "assistant_final_done",
                "slot_memory": {"payee_name": "小明", "amount": "200"},
                "message": "执行图已完成",
                "output": {},
            }

        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            response = await client.post(
                "/api/assistant/task/completion",
                json=AssistantTaskCompletionRequest(
                    sessionId="session_assistant_001",
                    taskId="task_001",
                    completionSignal=2,
                ).model_dump(mode="json", by_alias=True),
            )

        await router_client.aclose()

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "current_task": "task_001",
            "task_list": [{"name": "task_001", "status": "completed"}],
            "status": "completed",
            "intent_code": "AG_TRANS",
            "completion_state": 2,
            "completion_reason": "assistant_final_done",
            "slot_memory": {"payee_name": "小明", "amount": "200"},
            "message": "执行图已完成",
            "output": {},
        }
        assert received == {
            "payload": {
                "sessionId": "session_assistant_001",
                "taskId": "task_001",
                "completionSignal": 2,
                "stream": False,
            },
        }

    asyncio.run(run())


def test_assistant_service_proxies_router_stream_without_rewriting_events() -> None:
    async def run() -> None:
        router_app = FastAPI()
        received: dict[str, object] = {}
        expected_stream = (
            b"event: router_status\n"
            b'data: {"status":"processing","step":"intent_recognition"}\n\n'
            b"event: done\n"
            b"data: [DONE]\n\n"
        )

        @router_app.post("/api/v1/message")
        async def router_message_stream(payload: dict[str, object]) -> StreamingResponse:
            received["payload"] = payload

            async def event_generator():
                yield expected_stream

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            async with client.stream(
                "POST",
                "/api/assistant/run/stream",
                json=AssistantRunRequest(
                    sessionId="session_assistant_002",
                    txt="帮我查一下余额",
                    config_variables=[
                        {"name": "custID", "value": "C0002"},
                        {"name": "currentDisplay", "value": "account_home"},
                    ],
                    executionMode="router_only",
                    custId="C0002",
                ).model_dump(mode="json", by_alias=True),
            ) as response:
                body = await response.aread()
                headers = dict(response.headers)
                status_code = response.status_code

        await router_client.aclose()

        assert status_code == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"
        assert headers["x-accel-buffering"] == "no"
        assert body == expected_stream
        assert received == {
            "payload": {
                "sessionId": "session_assistant_002",
                "txt": "帮我查一下余额",
                "config_variables": [
                    {"name": "custID", "value": "C0002"},
                    {"name": "currentDisplay", "value": "account_home"},
                ],
                "executionMode": "router_only",
                "custId": "C0002",
                "stream": True,
            },
        }
        assert "cust_id" not in received["payload"]

    asyncio.run(run())


def test_assistant_service_proxies_router_task_completion_stream_without_rewriting_events() -> None:
    async def run() -> None:
        router_app = FastAPI()
        received: dict[str, object] = {}
        expected_stream = (
            b"event: message\n"
            b'data: {"ok":true,"status":"completed","completion_state":2,"completion_reason":"assistant_final_done"}\n\n'
            b"event: done\n"
            b"data: [DONE]\n\n"
        )

        @router_app.post("/api/v1/task/completion")
        async def router_task_completion_stream(payload: dict[str, object]) -> StreamingResponse:
            received["payload"] = payload

            async def event_generator():
                yield expected_stream

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            async with client.stream(
                "POST",
                "/api/assistant/task/completion/stream",
                json=AssistantTaskCompletionRequest(
                    sessionId="session_assistant_002",
                    taskId="task_002",
                    completionSignal=2,
                ).model_dump(mode="json", by_alias=True),
            ) as response:
                body = await response.aread()
                headers = dict(response.headers)
                status_code = response.status_code

        await router_client.aclose()

        assert status_code == 200
        assert headers["content-type"].startswith("text/event-stream")
        assert headers["cache-control"] == "no-cache"
        assert headers["x-accel-buffering"] == "no"
        assert body == expected_stream
        assert received == {
            "payload": {
                "sessionId": "session_assistant_002",
                "taskId": "task_002",
                "completionSignal": 2,
                "stream": True,
            },
        }

    asyncio.run(run())


def test_assistant_service_end_to_end_non_stream_with_real_router_app() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        router_app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        session_id = "assistant_e2e_001"
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            waiting = await client.post(
                "/api/assistant/run",
                json=AssistantRunRequest(
                    sessionId=session_id,
                    txt="给小明转账",
                    custId="C0001",
                    config_variables=[
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                ).model_dump(mode="json", by_alias=True),
            )
            waiting_assistant_completion = await client.post(
                "/api/assistant/run",
                json=AssistantRunRequest(
                    sessionId=session_id,
                    txt="200",
                    custId="C0001",
                    config_variables=[
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_confirm"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                ).model_dump(mode="json", by_alias=True),
            )

        assert waiting.status_code == 200
        waiting_body = waiting.json()
        assert waiting_body["ok"] is True
        assert waiting_body["status"] == "waiting_user_input"
        assert waiting_body["completion_state"] == 0
        assert waiting_body["current_task"].startswith("task_")
        assert waiting_body["task_list"] == [{"name": waiting_body["current_task"], "status": "waiting"}]
        assert waiting_body["slot_memory"] == {"payee_name": "小明"}
        assert waiting_body["output"] == {}

        assert waiting_assistant_completion.status_code == 200
        waiting_assistant_completion_body = waiting_assistant_completion.json()
        assert waiting_assistant_completion_body["ok"] is True
        assert waiting_assistant_completion_body["status"] == "waiting_assistant_completion"
        assert waiting_assistant_completion_body["completion_state"] == 1
        assert waiting_assistant_completion_body["completion_reason"] == "assistant_confirmation_required"
        assert waiting_assistant_completion_body["intent_code"] == "AG_TRANS"
        assert waiting_assistant_completion_body["message"] == "执行图等待助手确认完成态"
        assert waiting_assistant_completion_body["output"]["data"] == [
            {
                "isSubAgent": "True",
                "typIntent": "mbpTransfer",
                "answer": "||200|小明|",
            }
        ]
        assert waiting_assistant_completion_body["task_list"] == [
            {"name": waiting_assistant_completion_body["current_task"], "status": "waiting"}
        ]
        assert waiting_assistant_completion_body["output"]["completion_state"] == 2
        assert waiting_assistant_completion_body["output"]["completion_reason"] == "agent_final_done"

        completion_request = AssistantTaskCompletionRequest(
            sessionId=session_id,
            taskId=waiting_assistant_completion_body["current_task"],
            completionSignal=2,
        ).model_dump(mode="json", by_alias=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            completed = await client.post(
                "/api/assistant/task/completion",
                json=completion_request,
            )

        assert completed.status_code == 200
        completed_body = completed.json()
        assert completed_body["ok"] is True
        assert completed_body["status"] == "completed"
        assert completed_body["completion_state"] == 2
        assert completed_body["completion_reason"] == "assistant_final_done"
        assert completed_body["intent_code"] == "AG_TRANS"
        assert completed_body["message"] == "执行图已完成"
        assert completed_body["output"]["data"] == [
            {
                "isSubAgent": "True",
                "typIntent": "mbpTransfer",
                "answer": "||200|小明|",
            }
        ]
        assert completed_body["task_list"] == [
            {"name": waiting_assistant_completion_body["current_task"], "status": "completed"}
        ]
        assert len(agent_client.tasks) == 1
        assert agent_client.tasks[0].input_context["config_variables"] == {
            "custID": "C0001",
            "sessionID": session_id,
            "currentDisplay": "transfer_confirm",
            "agentSessionID": session_id,
        }

        await router_client.aclose()

    asyncio.run(run())


def test_assistant_service_end_to_end_stream_with_real_router_app() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        router_app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        router_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router_app),
            base_url="http://router.test",
        )
        session_id = "assistant_e2e_stream_001"
        service = RouterForwardService(
            router_base_url="http://router.test",
            http_client=router_client,
        )
        app = create_app()
        app.dependency_overrides[get_router_forward_service] = lambda: service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            async with client.stream(
                "POST",
                "/api/assistant/run/stream",
                json=AssistantRunRequest(
                    sessionId=session_id,
                    txt="给小明转账",
                    custId="C0001",
                    config_variables=[
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                ).model_dump(mode="json", by_alias=True),
            ) as response:
                waiting_text = "".join([chunk async for chunk in response.aiter_text()])

            async with client.stream(
                "POST",
                "/api/assistant/run/stream",
                json=AssistantRunRequest(
                    sessionId=session_id,
                    txt="200",
                    custId="C0001",
                    config_variables=[
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                ).model_dump(mode="json", by_alias=True),
            ) as response:
                waiting_assistant_completion_text = "".join([chunk async for chunk in response.aiter_text()])

        waiting_frames = _parse_sse_frames(waiting_text)
        assert waiting_frames[-1] == ("done", "[DONE]")
        waiting_message_payloads = _message_payloads(waiting_text)
        recognition_payload = waiting_message_payloads[0]
        assert recognition_payload["completion_reason"] == "intent_recognized"
        assert recognition_payload["intent_code"] == "AG_TRANS"
        waiting_payload = _non_recognition_payloads(waiting_message_payloads)[0]
        assert waiting_payload["status"] == "waiting_user_input"
        assert waiting_payload["completion_state"] == 0
        assert waiting_payload["current_task"].startswith("task_")
        assert waiting_payload["task_list"] == [{"name": waiting_payload["current_task"], "status": "waiting"}]
        assert waiting_payload["output"] == {}

        waiting_assistant_completion_frames = _parse_sse_frames(waiting_assistant_completion_text)
        assert waiting_assistant_completion_frames[-1] == ("done", "[DONE]")
        waiting_assistant_completion_payload = json.loads(waiting_assistant_completion_frames[0][1])
        assert waiting_assistant_completion_frames[0][0] == "message"
        assert waiting_assistant_completion_payload["status"] == "waiting_assistant_completion"
        assert waiting_assistant_completion_payload["completion_state"] == 1
        assert waiting_assistant_completion_payload["completion_reason"] == "assistant_confirmation_required"
        assert waiting_assistant_completion_payload["message"] == "执行图等待助手确认完成态"
        assert waiting_assistant_completion_payload["output"]["message"] == "已向小明转账 200 CNY，转账成功"
        assert waiting_assistant_completion_payload["output"]["data"][0]["answer"] == "||200|小明|"
        assert waiting_assistant_completion_payload["task_list"] == [
            {"name": waiting_assistant_completion_payload["current_task"], "status": "waiting"}
        ]

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://assistant.test",
        ) as client:
            async with client.stream(
                "POST",
                "/api/assistant/task/completion/stream",
                json=AssistantTaskCompletionRequest(
                    sessionId=session_id,
                    taskId=waiting_assistant_completion_payload["current_task"],
                    completionSignal=2,
                ).model_dump(mode="json", by_alias=True),
            ) as response:
                completed_text = "".join([chunk async for chunk in response.aiter_text()])

        await router_client.aclose()

        completed_frames = _parse_sse_frames(completed_text)
        assert completed_frames[-1] == ("done", "[DONE]")
        completed_payload = _non_recognition_payloads(_message_payloads(completed_text))[0]
        assert completed_payload["status"] == "completed"
        assert completed_payload["completion_state"] == 2
        assert completed_payload["completion_reason"] == "assistant_final_done"
        assert completed_payload["message"] == "执行图已完成"
        assert completed_payload["output"]["message"] == "已向小明转账 200 CNY，转账成功"
        assert completed_payload["output"]["data"][0]["answer"] == "||200|小明|"
        assert completed_payload["task_list"] == [
            {"name": waiting_assistant_completion_payload["current_task"], "status": "completed"}
        ]

    asyncio.run(run())
