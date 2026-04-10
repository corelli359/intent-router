from __future__ import annotations

import pytest

pytest.skip("Legacy V1 orchestrator coverage retired on dynamic graph runtime branch", allow_module_level=True)

import asyncio
import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from router_core.orchestrator import RouterOrchestrator  # noqa: E402
from router_core.domain import IntentDefinition, IntentMatch, TaskStatus  # noqa: E402
from router_core.orchestrator import RouterOrchestratorConfig  # noqa: E402
from router_core.recognizer import RecognitionResult  # noqa: E402
from tests.support.mock_agent_client import MockStreamingAgentClient  # noqa: E402


class StaticCatalog:
    def __init__(self) -> None:
        self._intents = [
            IntentDefinition(
                intent_code="query_account_balance",
                name="查询账户余额",
                description="查询账户余额",
                examples=["帮我查一下余额"],
                keywords=["余额", "账户"],
                agent_url="http://test-agent/query_account_balance",
                dispatch_priority=100,
                primary_threshold=0.68,
                candidate_threshold=0.45,
            )
        ]

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def priorities(self) -> dict[str, int]:
        return {intent.intent_code: intent.dispatch_priority for intent in self._intents}


class TransferCatalog:
    def __init__(self) -> None:
        self._intents = [
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="转账",
                examples=["帮我转账"],
                keywords=["转账"],
                agent_url="http://test-agent/transfer_money",
                dispatch_priority=100,
                primary_threshold=0.68,
                candidate_threshold=0.45,
            )
        ]

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def priorities(self) -> dict[str, int]:
        return {intent.intent_code: intent.dispatch_priority for intent in self._intents}


class StaticRecognizer:
    def __init__(self, intent_code: str) -> None:
        self.intent_code = intent_code

    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        active_codes = {intent.intent_code for intent in intents}
        if self.intent_code not in active_codes:
            return RecognitionResult(primary=[], candidates=[])
        return RecognitionResult(
            primary=[IntentMatch(intent_code=self.intent_code, confidence=0.96, reason="test recognizer")],
            candidates=[],
        )


def test_orchestrator_publishes_recognition_then_task_events() -> None:
    async def run() -> None:
        events = []

        def publish(event) -> None:
            events.append(event)

        orchestrator = RouterOrchestrator(
            publish_event=publish,
            intent_catalog=StaticCatalog(),
            recognizer=StaticRecognizer("query_account_balance"),
            agent_client=MockStreamingAgentClient(),
        )
        session = orchestrator.create_session(cust_id="cust_demo")
        await orchestrator.handle_user_message(session.session_id, "cust_demo", "帮我查一下余额")

        event_names = [event.event for event in events]
        assert "recognition.started" in event_names
        assert "recognition.completed" in event_names
        assert "task.created" in event_names
        assert "task.dispatching" in event_names
        assert "task.running" in event_names
        assert "task.waiting_user_input" in event_names
        assert event_names.index("recognition.started") < event_names.index("recognition.completed")
        assert event_names.index("recognition.completed") < event_names.index("task.created")
        assert event_names.index("task.created") < event_names.index("task.dispatching")

    asyncio.run(run())


def test_transfer_waiting_task_emits_resuming_before_completion() -> None:
    async def run() -> None:
        events: list[str] = []

        def publish(event) -> None:
            events.append(event.event)

        orchestrator = RouterOrchestrator(
            publish_event=publish,
            intent_catalog=TransferCatalog(),
            recognizer=StaticRecognizer("transfer_money"),
            agent_client=MockStreamingAgentClient(),
        )
        session = orchestrator.create_session(cust_id="cust_demo")

        await orchestrator.handle_user_message(session.session_id, "cust_demo", "帮我转账")
        await orchestrator.handle_user_message(session.session_id, "cust_demo", "5000 元")
        await orchestrator.handle_user_message(session.session_id, "cust_demo", "工资卡")
        await orchestrator.handle_user_message(session.session_id, "cust_demo", "确认")

        assert events.count("task.created") == 1
        assert "task.resuming" in events

    asyncio.run(run())


def test_agent_timeout_marks_task_failed() -> None:
    class HangingAgentClient:
        async def stream(self, task, user_input):
            await asyncio.sleep(0.05)
            if False:
                yield None

        async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
            return None

        async def close(self) -> None:
            return None

    async def run() -> None:
        events = []

        def publish(event) -> None:
            events.append(event)

        orchestrator = RouterOrchestrator(
            publish_event=publish,
            intent_catalog=StaticCatalog(),
            recognizer=StaticRecognizer("query_account_balance"),
            agent_client=HangingAgentClient(),
            config=RouterOrchestratorConfig(agent_timeout_seconds=0.01),
        )
        session = orchestrator.create_session(cust_id="cust_demo")

        snapshot = await orchestrator.handle_user_message(session.session_id, "cust_demo", "帮我查一下余额")

        assert snapshot.tasks[0].status == TaskStatus.FAILED
        assert any(event.event == "task.failed" and "超时" in (event.message or "") for event in events)

    asyncio.run(run())
