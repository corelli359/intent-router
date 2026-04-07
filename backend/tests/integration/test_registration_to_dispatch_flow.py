from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx


BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from admin_api.dependencies import get_intent_repository, get_settings  # noqa: E402
from app import create_app  # noqa: E402
from intent_agents.account_balance_service import AccountBalanceAgentService  # noqa: E402
from intent_agents.fallback_app import create_app as create_fallback_app  # noqa: E402
from intent_agents.order_status_app import create_app as create_order_status_app, get_order_status_service  # noqa: E402
from persistence.sql_intent_repository import DatabaseIntentRepository  # noqa: E402
from router_api.dependencies import get_orchestrator  # noqa: E402
from router_core.agent_client import StreamingAgentClient  # noqa: E402
from router_core.domain import IntentMatch  # noqa: E402
from router_core.intent_catalog import RepositoryIntentCatalog  # noqa: E402
from router_core.orchestrator import RouterOrchestrator  # noqa: E402
from router_core.recognizer import RecognitionResult  # noqa: E402


class HostRouterTransport(httpx.AsyncBaseTransport):
    def __init__(self, apps_by_host: dict[str, object]) -> None:
        self._transports = {
            host: httpx.ASGITransport(app=app)
            for host, app in apps_by_host.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._transports.get(host)
        if transport is None:
            raise RuntimeError(f"No ASGI app bound for host: {host}")
        return await transport.handle_async_request(request)


class RegistryAwareRecognizer:
    async def recognize(
        self,
        message: str,
        intents,
        recent_messages,
        long_term_memory,
        on_delta=None,
    ) -> RecognitionResult:
        if "余额" in message and any(intent.intent_code == "query_account_balance" for intent in intents):
            return RecognitionResult(
                primary=[
                    IntentMatch(
                        intent_code="query_account_balance",
                        confidence=0.92,
                        reason="integration recognizer matched balance query",
                    )
                ],
                candidates=[],
            )
        return RecognitionResult(primary=[], candidates=[])


def _digit_sequences(text: str) -> list[str]:
    sequences: list[str] = []
    current: list[str] = []
    for character in text:
        if character.isdigit():
            current.append(character)
            continue
        if current:
            sequences.append("".join(current))
            current = []
    if current:
        sequences.append("".join(current))
    return sequences


class DeterministicBalanceRunner:
    async def run_json(self, *, prompt, variables, schema=None):
        current_input = str(variables.get("input_text", ""))
        account_payload = json.loads(str(variables.get("account_json", "{}")))
        card_number = account_payload.get("cardNumber") or account_payload.get("card_number")
        phone_last4 = account_payload.get("phoneLast4") or account_payload.get("phone_last4")

        for sequence in _digit_sequences(current_input):
            if card_number is None and 12 <= len(sequence) <= 19:
                card_number = sequence
                continue
            if len(sequence) == 4:
                phone_last4 = sequence

        has_enough_information = bool(card_number and phone_last4)
        return {
            "card_number": card_number,
            "phone_last4": phone_last4,
            "has_enough_information": has_enough_information,
            "ask_message": "" if has_enough_information else "请提供卡号和手机号后4位",
        }


def test_register_activate_and_route_via_database_repository(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        database_url = f"sqlite:///{tmp_path / 'intent-router.db'}"
        monkeypatch.setenv("ADMIN_REPOSITORY_BACKEND", "database")
        monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
        monkeypatch.setenv("ROUTER_RECOGNIZER_BACKEND", "llm")
        monkeypatch.setenv("ROUTER_INTENT_REFRESH_INTERVAL_SECONDS", "0.01")

        get_settings.cache_clear()
        get_intent_repository.cache_clear()
        repository = get_intent_repository()
        assert isinstance(repository, DatabaseIntentRepository)

        order_app = create_order_status_app()
        order_app.dependency_overrides[get_order_status_service] = lambda: AccountBalanceAgentService(
            resolver=DeterministicBalanceRunner()
        )
        agent_transport = HostRouterTransport(
            {
                "intent-order-agent": order_app,
                "intent-fallback-agent": create_fallback_app(),
            }
        )
        agent_http_client = httpx.AsyncClient(transport=agent_transport)
        catalog = RepositoryIntentCatalog(repository, refresh_interval_seconds=0.01)
        orchestrator = RouterOrchestrator(
            publish_event=lambda event: None,
            intent_catalog=catalog,
            recognizer=RegistryAwareRecognizer(),
            agent_client=StreamingAgentClient(http_client=agent_http_client),
        )

        app = create_app()
        app.dependency_overrides[get_orchestrator] = lambda: orchestrator

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                register_order = await client.post(
                    "/api/admin/intents",
                    json={
                        "intent_code": "query_account_balance",
                        "name": "查询账户余额",
                        "description": "查询账户余额和账户可用额度",
                        "examples": ["帮我查下账户余额", "查一下余额"],
                        "agent_url": "http://intent-order-agent/api/agent/run",
                        "status": "inactive",
                        "is_fallback": False,
                        "dispatch_priority": 100,
                        "request_schema": {
                            "type": "object",
                            "required": ["sessionId", "taskId", "input", "conversation.recentMessages"],
                        },
                        "field_mapping": {
                            "sessionId": "$session.id",
                            "taskId": "$task.id",
                            "input": "$message.current",
                            "conversation.recentMessages": "$context.recent_messages",
                            "conversation.longTermMemory": "$context.long_term_memory",
                            "account.cardNumber": "$slot_memory.card_number",
                            "account.phoneLast4": "$slot_memory.phone_last_four",
                        },
                        "resume_policy": "resume_same_task",
                    },
                )
                assert register_order.status_code == 201

                register_fallback = await client.post(
                    "/api/admin/intents",
                    json={
                        "intent_code": "fallback_general",
                        "name": "通用兜底",
                        "description": "当前消息未命中任何已注册意图时，负责澄清用户诉求。",
                        "examples": ["我想处理一个系统暂未识别的请求"],
                        "agent_url": "http://intent-fallback-agent/api/agent/run",
                        "status": "inactive",
                        "is_fallback": True,
                        "dispatch_priority": 1,
                        "request_schema": {
                            "type": "object",
                            "required": ["sessionId", "taskId", "input"],
                        },
                        "field_mapping": {
                            "sessionId": "$session.id",
                            "taskId": "$task.id",
                            "input": "$message.current",
                            "conversation.recentMessages": "$context.recent_messages",
                            "conversation.longTermMemory": "$context.long_term_memory",
                        },
                        "resume_policy": "resume_same_task",
                    },
                )
                assert register_fallback.status_code == 201

                activate_order = await client.post("/api/admin/intents/query_account_balance/activate")
                activate_fallback = await client.post("/api/admin/intents/fallback_general/activate")
                assert activate_order.status_code == 200
                assert activate_fallback.status_code == 200

                persisted = DatabaseIntentRepository(database_url)
                assert [item.intent_code for item in persisted.list_intents()] == [
                    "query_account_balance",
                    "fallback_general",
                ]

                session_id = (await client.post("/api/router/sessions")).json()["session_id"]

                first_turn = await client.post(
                    f"/api/router/sessions/{session_id}/messages",
                    json={"content": "帮我查下账户余额"},
                )
                assert first_turn.status_code == 200
                first_snapshot = first_turn.json()["snapshot"]
                assert first_snapshot["tasks"][0]["intent_code"] == "query_account_balance"
                assert first_snapshot["tasks"][0]["status"] == "waiting_user_input"

                second_turn = await client.post(
                    f"/api/router/sessions/{session_id}/messages",
                    json={"content": "卡号 6222021234567890，手机号后四位 1234"},
                )
                assert second_turn.status_code == 200
                second_snapshot = second_turn.json()["snapshot"]
                assert second_snapshot["tasks"][0]["intent_code"] == "query_account_balance"
                assert second_snapshot["tasks"][0]["status"] == "completed"

                fallback_session_id = (await client.post("/api/router/sessions")).json()["session_id"]
                fallback_turn = await client.post(
                    f"/api/router/sessions/{fallback_session_id}/messages",
                    json={"content": "我想随便聊聊"},
                )
                assert fallback_turn.status_code == 200
                fallback_snapshot = fallback_turn.json()["snapshot"]
                assert fallback_snapshot["tasks"][0]["intent_code"] == "fallback_general"
                assert fallback_snapshot["tasks"][0]["status"] == "waiting_user_input"
        finally:
            await agent_http_client.aclose()
            get_settings.cache_clear()
            get_intent_repository.cache_clear()

    asyncio.run(run())
