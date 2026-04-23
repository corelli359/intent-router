from __future__ import annotations

import asyncio

from router_service.core.graph.planner import TurnDecisionPayload
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.recognition.understanding_service import IntentUnderstandingService
from router_service.core.shared.domain import ChatMessage, IntentDefinition, IntentMatch
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState, GraphSessionState


class _Catalog:
    def __init__(self, intents: list[IntentDefinition]) -> None:
        self._intents = intents

    def active_intents_by_code(self):
        return {intent.intent_code: intent for intent in self._intents}


class _FullRecognizerShouldNotRun:
    def __init__(self) -> None:
        self.calls = 0
        self.last_recent_messages: list[str] | None = None
        self.last_long_term_memory: list[str] | None = None

    async def recognize(self, *args, **kwargs):
        del args
        self.calls += 1
        self.last_recent_messages = list(kwargs.get("recent_messages", []))
        self.last_long_term_memory = list(kwargs.get("long_term_memory", []))
        return RecognitionResult(
            primary=[
                IntentMatch(
                    intent_code="transfer_money",
                    confidence=0.96,
                    reason="llm recognized transfer intent",
                )
            ],
            candidates=[],
            diagnostics=[],
        )


class _TurnInterpreter:
    async def interpret_pending_graph(self, *, message, pending_graph, recognition):
        del message, pending_graph, recognition
        return TurnDecisionPayload(action="wait")

    async def interpret_waiting_node(self, *, message, waiting_node, current_graph, recognition):
        del message, waiting_node, current_graph, recognition
        return TurnDecisionPayload(action="wait")


class _EventPublisher:
    async def publish_recognition_started(self, session):
        del session

    async def publish_recognition_delta(self, session, *, delta: str):
        del session, delta

    async def publish_recognition_completed(self, session, *, recognition):
        del session, recognition

    async def publish_graph_builder_started(self, session):
        del session

    async def publish_graph_builder_delta(self, session, *, delta: str):
        del session, delta

    async def publish_graph_builder_completed(self, session, *, result):
        del session, result


def test_waiting_node_turn_uses_full_recognizer_when_no_local_fast_path_exists() -> None:
    async def run() -> None:
        intents = [
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账",
                examples=["给张三转 200 元"],
                keywords=["转账", "汇款"],
                agent_url="https://agent.example.com/transfer",
                dispatch_priority=100,
                primary_threshold=0.7,
                candidate_threshold=0.5,
            )
        ]
        recognizer = _FullRecognizerShouldNotRun()
        service = IntentUnderstandingService(
            intent_catalog=_Catalog(intents),
            recognizer=recognizer,
            graph_builder=None,
            turn_interpreter=_TurnInterpreter(),
            event_publisher=_EventPublisher(),
        )
        session = GraphSessionState(session_id="session-fast", cust_id="cust-fast")
        session.messages.extend(
            [
                ChatMessage(role="user", content="我要转账"),
                ChatMessage(role="assistant", content="请提供金额、收款人姓名"),
                ChatMessage(role="user", content="给张三转200"),
            ]
        )
        graph = ExecutionGraphState(source_message="帮我转账")
        node = GraphNodeState(
            intent_code="transfer_money",
            title="转账",
            confidence=0.9,
            position=0,
            source_fragment="帮我转账",
        )

        result = await service.interpret_waiting_node_turn(
            session,
            waiting_node=node,
            current_graph=graph,
            content="给张三转200",
        )

        assert [match.intent_code for match in result.recognition.primary] == ["transfer_money"]
        assert recognizer.calls == 1
        assert recognizer.last_recent_messages == [
            "user: 我要转账",
            "assistant: 请提供金额、收款人姓名",
        ]
        assert recognizer.last_long_term_memory == []

    asyncio.run(run())
