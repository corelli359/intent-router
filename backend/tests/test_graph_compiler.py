from __future__ import annotations

import asyncio

from router_service.core.graph.compiler import GraphCompiler
from router_service.core.graph.planner import SequentialIntentGraphPlanner
from router_service.core.support.context_builder import ContextBuilder
from router_service.core.shared.domain import ChatMessage
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.shared.domain import IntentDefinition, IntentMatch
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState, GraphSessionState, GraphStatus
from router_service.core.slots.resolution_service import SlotResolutionService


class _StaticCatalog:
    def __init__(self, intents: list[IntentDefinition]) -> None:
        self._intents = list(intents)
        self._index = {intent.intent_code: intent for intent in intents}

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def active_intents_by_code(self) -> dict[str, IntentDefinition]:
        return dict(self._index)

    def get_fallback_intent(self) -> IntentDefinition | None:
        return None


class _PassiveUnderstandingService:
    has_graph_builder = False


class _CapturingUnderstandingService:
    has_graph_builder = False

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recognize_message(
        self,
        session,
        content,
        *,
        recent_messages,
        long_term_memory,
        emit_events,
    ):
        self.calls.append(
            {
                "session_id": session.session_id,
                "content": content,
                "recent_messages": list(recent_messages),
                "long_term_memory": list(long_term_memory),
                "emit_events": emit_events,
            }
        )
        return RecognitionResult(primary=[], candidates=[])


class _RecordingRecognitionPublisher:
    def __init__(self) -> None:
        self.completed: list[list[str]] = []

    async def publish_recognition_completed(self, session, *, recognition) -> None:
        del session
        self.completed.append([match.intent_code for match in recognition.primary])


class _MisorderedUnderstandingService:
    has_graph_builder = False

    def __init__(self) -> None:
        self.event_publisher = _RecordingRecognitionPublisher()
        self.emit_event_flags: list[bool] = []

    async def recognize_message(
        self,
        session,
        content,
        *,
        recent_messages,
        long_term_memory,
        emit_events,
    ):
        del session, content, recent_messages, long_term_memory
        self.emit_event_flags.append(emit_events)
        return RecognitionResult(
            primary=[
                IntentMatch(intent_code="transfer_money", confidence=0.92, reason="raw recognizer order"),
                IntentMatch(intent_code="query_account_balance", confidence=0.97, reason="raw recognizer order"),
            ],
            candidates=[],
        )


class _SpyPlanner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.delegate = SequentialIntentGraphPlanner()

    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ):
        self.calls.append(
            {
                "message": message,
                "match_count": len(matches),
                "intent_codes": [match.intent_code for match in matches],
            }
        )
        return await self.delegate.plan(
            message=message,
            matches=matches,
            intents_by_code=intents_by_code,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
        )


class _GraphOrderPlanner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ):
        del message, intents_by_code, recent_messages, long_term_memory
        self.calls.append([match.intent_code for match in matches])
        graph = ExecutionGraphState(
            source_message="先查余额，再转账",
            summary="按执行图顺序先查余额再转账",
            status=GraphStatus.WAITING_CONFIRMATION,
        )
        graph.nodes.extend(
            [
                GraphNodeState(
                    intent_code="query_account_balance",
                    title="查询账户余额",
                    confidence=0.97,
                    position=0,
                    source_fragment="先查余额",
                ),
                GraphNodeState(
                    intent_code="transfer_money",
                    title="转账",
                    confidence=0.92,
                    position=1,
                    source_fragment="再转账",
                ),
            ]
        )
        return graph


def _transfer_intent(*, confirm_policy: str = "auto") -> IntentDefinition:
    return IntentDefinition(
        intent_code="transfer_money",
        name="转账",
        description="执行转账。",
        examples=["给小红转200"],
        keywords=["转账"],
        agent_url="http://agent.example.com/transfer",
        dispatch_priority=100,
        primary_threshold=0.75,
        candidate_threshold=0.5,
        slot_schema=[
            {
                "slot_key": "recipient_name",
                "label": "收款人",
                "description": "收款人姓名",
                "value_type": "person_name",
                "required": True,
            },
            {
                "slot_key": "amount",
                "label": "金额",
                "description": "转账金额",
                "value_type": "currency",
                "required": True,
            },
        ],
        graph_build_hints={
            "intent_scope_rule": "单次转账动作是一个 intent。",
            "planner_notes": "简单单次转账不要拆成多个节点。",
            "confirm_policy": confirm_policy,
        },
    )


def _structured_multi_step_transfer_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="transfer_money",
        name="转账",
        description="执行转账。",
        examples=["先给小红转200，再给小明转300"],
        keywords=["转账"],
        agent_url="http://agent.example.com/transfer",
        dispatch_priority=100,
        primary_threshold=0.75,
        candidate_threshold=0.5,
        slot_schema=[],
        graph_build_hints={
            "multi_node_examples": ["先给小红转200，再给小明转300"],
            "confirm_policy": "auto",
            "max_nodes_per_message": 4,
        },
    )


def _balance_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="query_account_balance",
        name="查询账户余额",
        description="查询账户余额。",
        examples=["帮我查一下余额"],
        keywords=["余额"],
        agent_url="http://agent.example.com/balance",
        dispatch_priority=90,
        primary_threshold=0.75,
        candidate_threshold=0.5,
        slot_schema=[],
        graph_build_hints={
            "intent_scope_rule": "单次余额查询是一个 intent。",
            "planner_notes": "普通余额查询不需要额外拆节点。",
            "confirm_policy": "auto",
        },
    )


def _compiler(
    *,
    intents: list[IntentDefinition],
    planning_policy: str,
    heavy_planner: _SpyPlanner | None = None,
    fallback_planner: _SpyPlanner | None = None,
) -> tuple[GraphCompiler, _SpyPlanner, _SpyPlanner]:
    heavy = heavy_planner or _SpyPlanner()
    fallback = fallback_planner or _SpyPlanner()
    compiler = GraphCompiler(
        intent_catalog=_StaticCatalog(intents),
        planner=heavy,
        understanding_service=_PassiveUnderstandingService(),
        slot_resolution_service=SlotResolutionService(),
        planning_policy=planning_policy,
        fallback_planner=fallback,
    )
    return compiler, heavy, fallback


async def _compile_with_matches(
    compiler: GraphCompiler,
    *,
    message: str,
    matches: list[IntentMatch],
):
    session = GraphSessionState(session_id="session_test", cust_id="cust_demo")
    return await compiler.compile_message(
        session,
        message,
        build_session_context=lambda _session: {"recent_messages": [], "long_term_memory": []},
        sanitize_recent_messages_for_planning=lambda entries: entries,
        recognition=RecognitionResult(primary=matches, candidates=[]),
        recent_messages=[],
        long_term_memory=[],
    )


def test_graph_compiler_auto_policy_skips_heavy_planner_for_simple_single_intent() -> None:
    compiler, heavy, fallback = _compiler(
        intents=[_transfer_intent()],
        planning_policy="auto",
    )

    result = asyncio.run(
        _compile_with_matches(
            compiler,
            message="给小红转200",
            matches=[IntentMatch(intent_code="transfer_money", confidence=0.96, reason="single intent")],
        )
    )

    assert len(heavy.calls) == 0
    assert len(fallback.calls) == 1
    assert len(result.graph.nodes) == 1
    assert result.graph.nodes[0].intent_code == "transfer_money"
    assert result.graph.status == GraphStatus.DRAFT


def test_graph_compiler_auto_policy_skips_heavy_planner_for_complex_wording_without_structured_hints() -> None:
    compiler, heavy, fallback = _compiler(
        intents=[_transfer_intent()],
        planning_policy="auto",
    )

    asyncio.run(
        _compile_with_matches(
            compiler,
            message="给小红转200，再给小明转300",
            matches=[IntentMatch(intent_code="transfer_money", confidence=0.96, reason="single intent")],
        )
    )

    assert len(heavy.calls) == 0
    assert len(fallback.calls) == 1


def test_graph_compiler_auto_policy_uses_heavy_planner_when_catalog_declares_multi_step_examples() -> None:
    compiler, heavy, fallback = _compiler(
        intents=[_structured_multi_step_transfer_intent()],
        planning_policy="auto",
    )

    asyncio.run(
        _compile_with_matches(
            compiler,
            message="先给小红转200，再给小明转300",
            matches=[IntentMatch(intent_code="transfer_money", confidence=0.96, reason="single intent")],
        )
    )

    assert len(heavy.calls) == 1
    assert len(fallback.calls) == 0


def test_graph_compiler_multi_intent_only_policy_uses_heavy_planner_for_multi_intent() -> None:
    compiler, heavy, fallback = _compiler(
        intents=[_balance_intent(), _transfer_intent()],
        planning_policy="multi_intent_only",
    )

    result = asyncio.run(
        _compile_with_matches(
            compiler,
            message="查一下余额再给小红转200",
            matches=[
                IntentMatch(intent_code="query_account_balance", confidence=0.94, reason="balance"),
                IntentMatch(intent_code="transfer_money", confidence=0.92, reason="transfer"),
            ],
        )
    )

    assert len(heavy.calls) == 1
    assert len(fallback.calls) == 0
    assert len(result.graph.nodes) == 2


def test_graph_compiler_defers_recognition_event_until_it_matches_graph_order() -> None:
    understanding_service = _MisorderedUnderstandingService()
    planner = _GraphOrderPlanner()
    compiler = GraphCompiler(
        intent_catalog=_StaticCatalog([_balance_intent(), _transfer_intent()]),
        planner=planner,
        understanding_service=understanding_service,
        slot_resolution_service=SlotResolutionService(),
        planning_policy="always",
    )
    session = GraphSessionState(session_id="session_graph_order", cust_id="cust_demo")

    result = asyncio.run(
        compiler.compile_message(
            session,
            "先查余额，再转账",
            build_session_context=lambda _session: {"recent_messages": [], "long_term_memory": []},
            sanitize_recent_messages_for_planning=lambda entries: entries,
            emit_events=True,
        )
    )

    assert understanding_service.emit_event_flags == [False]
    assert planner.calls == [["transfer_money", "query_account_balance"]]
    assert [match.intent_code for match in result.recognition.primary] == [
        "query_account_balance",
        "transfer_money",
    ]
    assert understanding_service.event_publisher.completed == [
        ["query_account_balance", "transfer_money"]
    ]


def test_graph_compiler_direct_single_intent_honors_confirm_policy() -> None:
    compiler, heavy, fallback = _compiler(
        intents=[_transfer_intent(confirm_policy="always")],
        planning_policy="auto",
    )

    result = asyncio.run(
        _compile_with_matches(
            compiler,
            message="给小红转200",
            matches=[IntentMatch(intent_code="transfer_money", confidence=0.96, reason="single intent")],
        )
    )

    assert len(heavy.calls) == 0
    assert len(fallback.calls) == 1
    assert result.graph.status == GraphStatus.WAITING_CONFIRMATION
    assert [action.code for action in result.graph.actions] == ["confirm_graph", "cancel_graph"]


def test_graph_compiler_recognize_only_passes_recent_messages_and_memory_from_context() -> None:
    understanding_service = _CapturingUnderstandingService()
    compiler = GraphCompiler(
        intent_catalog=_StaticCatalog([_transfer_intent()]),
        planner=_SpyPlanner(),
        understanding_service=understanding_service,
        slot_resolution_service=SlotResolutionService(),
        planning_policy="auto",
    )
    session = GraphSessionState(
        session_id="session_memory",
        cust_id="cust_memory",
        messages=[
            ChatMessage(role="user", content="我要转账"),
            ChatMessage(role="assistant", content="请提供金额"),
        ],
    )
    context_builder = ContextBuilder()

    asyncio.run(
        compiler.recognize_only(
            session,
            "200",
            build_session_context=lambda current_session: context_builder.build_task_context(
                current_session,
                task=None,
                long_term_memory=["payee_name=小明"],
            ),
            sanitize_recent_messages_for_planning=lambda entries: entries,
            emit_events=False,
        )
    )

    assert understanding_service.calls == [
        {
            "session_id": "session_memory",
            "content": "200",
            "recent_messages": [
                "user: 我要转账",
                "assistant: 请提供金额",
            ],
            "long_term_memory": ["payee_name=小明"],
            "emit_events": False,
        }
    ]
