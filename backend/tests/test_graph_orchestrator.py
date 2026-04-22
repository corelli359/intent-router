from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from router_service.core.graph.orchestrator import GraphRouterOrchestrator, GraphRouterOrchestratorConfig
from router_service.core.shared.domain import IntentDefinition, Task, TaskStatus
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphNodeStatus,
    GraphSessionState,
    GraphStatus,
)
from router_service.core.slots.understanding_validator import UnderstandingValidationResult


class _SingleIntentCatalog:
    def __init__(self, intent: IntentDefinition) -> None:
        self._intent = intent

    def active_intents_by_code(self) -> dict[str, IntentDefinition]:
        return {self._intent.intent_code: self._intent}


class _SpyUnderstandingValidator:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def validate_node(
        self,
        *,
        intent,
        node,
        graph_source_message,
        current_message,
        long_term_memory=None,
    ) -> UnderstandingValidationResult:
        self.calls.append(
            {
                "intent_code": intent.intent_code,
                "node_id": node.node_id,
                "graph_source_message": graph_source_message,
                "current_message": current_message,
                "long_term_memory": list(long_term_memory or []),
            }
        )
        return UnderstandingValidationResult(
            slot_memory=dict(node.slot_memory),
            slot_bindings=[],
            history_slot_keys=[],
            missing_required_slots=[],
            ambiguous_slot_keys=[],
            invalid_slot_keys=[],
            needs_confirmation=False,
            can_dispatch=True,
            prompt_message=None,
            diagnostics=[],
        )


def test_graph_orchestrator_drain_guard_fails_non_converging_graph() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(
            publish_event=lambda event: None,
            config=GraphRouterOrchestratorConfig(
                max_drain_iterations=2,
            ),
        )
        graph = ExecutionGraphState(source_message="测试 graph drain 护栏")
        node = GraphNodeState(
            intent_code="query_account_balance",
            title="查询余额",
            confidence=0.95,
            position=0,
            status=GraphNodeStatus.READY,
        )
        graph.nodes.append(node)
        session = GraphSessionState(
            session_id="session_guard",
            cust_id="cust_guard",
            current_graph=graph,
        )
        published_graph_events: list[tuple[str, str]] = []
        published_session_events: list[str] = []

        async def refresh_graph_state(session_arg: GraphSessionState, graph_arg: ExecutionGraphState) -> None:
            node.touch(GraphNodeStatus.READY)

        async def run_node(
            session_arg: GraphSessionState,
            graph_arg: ExecutionGraphState,
            node_arg: GraphNodeState,
            seed_input: str,
        ) -> None:
            node_arg.touch(GraphNodeStatus.COMPLETED)

        async def publish_graph_state(
            session_arg: GraphSessionState,
            event: str,
            message: str,
            *,
            status=None,
        ) -> None:
            published_graph_events.append((event, message))

        async def publish_session_state(session_arg: GraphSessionState, event: str) -> None:
            published_session_events.append(event)

        orchestrator._refresh_graph_state = AsyncMock(side_effect=refresh_graph_state)
        orchestrator._run_node = AsyncMock(side_effect=run_node)
        orchestrator._publish_graph_state = AsyncMock(side_effect=publish_graph_state)
        orchestrator._publish_session_state = AsyncMock(side_effect=publish_session_state)
        orchestrator._get_waiting_node = lambda session_arg: None
        orchestrator._next_ready_node = lambda graph_arg: node if node.status == GraphNodeStatus.READY else None

        await orchestrator._drain_graph(session, graph.source_message)

        assert graph.status == GraphStatus.FAILED
        assert session.active_node_id is None
        assert any("超过最大迭代次数" in message.content for message in session.messages)
        assert published_graph_events == [("graph.failed", session.messages[-1].content)]
        assert published_session_events == ["session.idle"]

    asyncio.run(run())


def test_graph_orchestrator_skips_full_refresh_for_non_terminal_agent_chunk() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        session = GraphSessionState(session_id="session_chunk", cust_id="cust_chunk")
        graph = ExecutionGraphState(source_message="测试 chunk")
        node = GraphNodeState(
            intent_code="transfer_money",
            title="转账",
            confidence=0.95,
            position=0,
            status=GraphNodeStatus.RUNNING,
        )
        task = Task(
            session_id=session.session_id,
            intent_code=node.intent_code,
            agent_url="https://agent.example.com/run",
            intent_name=node.title,
            intent_description="desc",
            confidence=node.confidence,
        )
        chunk = SimpleNamespace(
            status=TaskStatus.RUNNING,
            content="处理中",
            payload={},
            ishandover=False,
            output={},
        )

        orchestrator.event_publisher.publish_node_runtime_event = AsyncMock()
        orchestrator._refresh_graph_state = AsyncMock()
        orchestrator._emit_graph_progress = AsyncMock()

        await orchestrator._handle_agent_chunk(session, graph, node, task, chunk)

        orchestrator.event_publisher.publish_node_runtime_event.assert_awaited_once()
        orchestrator._refresh_graph_state.assert_not_awaited()
        orchestrator._emit_graph_progress.assert_not_awaited()

    asyncio.run(run())


def test_graph_orchestrator_refreshes_graph_for_terminal_agent_chunk() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        session = GraphSessionState(session_id="session_chunk_terminal", cust_id="cust_chunk")
        graph = ExecutionGraphState(source_message="测试 chunk")
        node = GraphNodeState(
            intent_code="transfer_money",
            title="转账",
            confidence=0.95,
            position=0,
            status=GraphNodeStatus.RUNNING,
        )
        task = Task(
            session_id=session.session_id,
            intent_code=node.intent_code,
            agent_url="https://agent.example.com/run",
            intent_name=node.title,
            intent_description="desc",
            confidence=node.confidence,
        )
        chunk = SimpleNamespace(
            status=TaskStatus.COMPLETED,
            content="已完成",
            payload={},
            ishandover=True,
            output={},
        )

        orchestrator.event_publisher.publish_node_runtime_event = AsyncMock()
        orchestrator._refresh_graph_state = AsyncMock()
        orchestrator._emit_graph_progress = AsyncMock()

        await orchestrator._handle_agent_chunk(session, graph, node, task, chunk)

        orchestrator._refresh_graph_state.assert_awaited_once()
        orchestrator._emit_graph_progress.assert_awaited_once()

    asyncio.run(run())


def test_graph_orchestrator_serialized_message_response_preserves_handover_graph_before_cleanup() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        graph = ExecutionGraphState(
            source_message="转 500 给王芳的卡",
            summary="router_only handover",
            status=GraphStatus.READY_FOR_DISPATCH,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="transfer_money",
                title="转账",
                confidence=0.96,
                position=0,
                status=GraphNodeStatus.READY_FOR_DISPATCH,
                slot_memory={
                    "amount": "500",
                    "payee_name": "王芳",
                },
            )
        )

        async def handle_user_message(
            session_id: str,
            cust_id: str,
            content: str,
            *,
            assistant_protocol: bool,
            router_only: bool,
            guided_selection=None,
            recommendation_context=None,
            proactive_recommendation=None,
            return_snapshot: bool,
            emit_events: bool,
        ) -> None:
            del assistant_protocol, content, guided_selection, recommendation_context, proactive_recommendation, return_snapshot
            assert emit_events is False
            session = orchestrator.session_store.get_or_create(session_id, cust_id)
            session.attach_business(graph, router_only_mode=router_only, pending=False)
            session.touch()

        orchestrator.message_flow.handle_user_message = AsyncMock(side_effect=handle_user_message)

        payload = await orchestrator.handle_user_message_serialized(
            session_id="session_router_only",
            cust_id="cust_router_only",
            content="转 500 给王芳的卡",
            router_only=True,
            serializer=lambda session: {
                "current_graph_status": session.current_graph.status.value if session.current_graph is not None else None,
                "node_status": session.current_graph.nodes[0].status.value if session.current_graph is not None else None,
                "slot_memory": dict(session.current_graph.nodes[0].slot_memory) if session.current_graph is not None else {},
                "shared_slot_memory": dict(session.shared_slot_memory),
            },
        )

        session = orchestrator.session_store.get("session_router_only")

        assert payload == {
            "current_graph_status": "ready_for_dispatch",
            "node_status": "ready_for_dispatch",
            "slot_memory": {
                "amount": "500",
                "payee_name": "王芳",
            },
            "shared_slot_memory": {},
        }
        assert session.current_graph is None
        assert session.pending_graph is None
        assert session.shared_slot_memory == {
            "amount": "500",
            "payee_name": "王芳",
        }
        assert session.business_memory_digests[-1].status == "ready_for_dispatch"

    asyncio.run(run())


def test_graph_orchestrator_stream_message_path_enables_emit_events_by_default() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        captured: dict[str, object] = {}

        async def handle_user_message(
            session_id: str,
            cust_id: str,
            content: str,
            *,
            assistant_protocol: bool,
            router_only: bool,
            guided_selection=None,
            recommendation_context=None,
            proactive_recommendation=None,
            return_snapshot: bool,
            emit_events: bool,
        ) -> None:
            del assistant_protocol, content, guided_selection, recommendation_context, proactive_recommendation, return_snapshot
            captured["emit_events"] = emit_events
            session = orchestrator.session_store.get_or_create(session_id, cust_id)
            session.router_only_mode = router_only
            session.touch()

        orchestrator.message_flow.handle_user_message = AsyncMock(side_effect=handle_user_message)

        payload = await orchestrator.handle_user_message(
            session_id="session_stream_like",
            cust_id="cust_stream_like",
            content="给王芳转 100 元",
            router_only=True,
        )

        assert payload is not None
        assert captured["emit_events"] is True

    asyncio.run(run())


def test_graph_orchestrator_validate_node_understanding_passes_history_memory_candidates() -> None:
    async def run() -> None:
        intent = IntentDefinition(
            intent_code="AG_TRANS",
            name="转账",
            description="执行转账",
            agent_url="http://agent.example.com/transfer",
            slot_schema=[
                {
                    "slot_key": "payee_name",
                    "label": "收款人",
                    "description": "收款人姓名",
                    "value_type": "string",
                    "required": False,
                    "allow_from_history": True,
                }
            ],
        )
        spy_validator = _SpyUnderstandingValidator()
        orchestrator = GraphRouterOrchestrator(
            publish_event=lambda event: None,
            intent_catalog=_SingleIntentCatalog(intent),
            understanding_validator=spy_validator,
        )
        orchestrator.session_store.long_term_memory.recall = lambda cust_id, limit: ["memory_fact"]  # type: ignore[method-assign]
        session = GraphSessionState(
            session_id="session_memory_slots",
            cust_id="cust_memory_slots",
            shared_slot_memory={"payee_name": "小明"},
        )
        graph = ExecutionGraphState(source_message="我要转账")
        node = GraphNodeState(
            intent_code="AG_TRANS",
            title="转账",
            confidence=0.97,
            position=0,
            source_fragment="我要转账",
        )

        await orchestrator._validate_node_understanding(
            session,
            graph,
            node,
            intent=intent,
            current_message="200",
        )

        assert spy_validator.calls == [
            {
                "intent_code": "AG_TRANS",
                "node_id": node.node_id,
                "graph_source_message": "我要转账",
                "current_message": "200",
                "long_term_memory": [
                    "memory_fact",
                    "payee_name=小明",
                ],
            }
        ]

    asyncio.run(run())


def test_graph_orchestrator_task_completion_serialized_returns_task_scoped_payload_before_cleanup() -> None:
    async def run() -> None:
        orchestrator = GraphRouterOrchestrator(publish_event=lambda event: None)
        graph = ExecutionGraphState(
            source_message="给小明转200",
            summary="transfer",
            status=GraphStatus.RUNNING,
        )
        node = GraphNodeState(
            intent_code="AG_TRANS",
            title="转账",
            confidence=0.96,
            position=0,
            status=GraphNodeStatus.WAITING_USER_INPUT,
            slot_memory={"payee_name": "小明", "amount": "200"},
        )
        graph.nodes.append(node)
        session = orchestrator.session_store.create(cust_id="cust_task_completion", session_id="session_task_completion")
        session.attach_business(graph, router_only_mode=False, pending=False)
        task = Task(
            session_id=session.session_id,
            intent_code="AG_TRANS",
            agent_url="http://agent.example.com/transfer",
            intent_name="转账",
            intent_description="执行转账",
            confidence=0.96,
            status=TaskStatus.WAITING_USER_INPUT,
            slot_memory={"payee_name": "小明", "amount": "200"},
        )
        task.touch(TaskStatus.WAITING_USER_INPUT)
        session.tasks.append(task)
        node.task_id = task.task_id

        payload = await orchestrator.handle_task_completion_serialized(
            session_id=session.session_id,
            task_id=task.task_id,
            completion_signal=2,
            serializer=lambda current_session: {
                "current_graph_status": current_session.current_graph.status.value if current_session.current_graph else None,
                "node_status": current_session.current_graph.nodes[0].status.value if current_session.current_graph else None,
                "shared_slot_memory": dict(current_session.shared_slot_memory),
                "task_count": len(current_session.tasks),
                "completion_override": current_session.current_graph.nodes[0].completion_override() if current_session.current_graph else None,
            },
            emit_events=False,
        )

        session_after = orchestrator.session_store.get(session.session_id)

        assert payload == {
            "current_graph_status": "completed",
            "node_status": "completed",
            "shared_slot_memory": {},
            "task_count": 1,
            "completion_override": (2, "assistant_final_done"),
        }
        assert session_after.current_graph is None
        assert session_after.pending_graph is None
        assert session_after.shared_slot_memory == {
            "payee_name": "小明",
            "amount": "200",
        }
        assert session_after.tasks == []
        assert session_after.business_memory_digests[-1].status == "completed"

    asyncio.run(run())


def test_graph_orchestrator_create_task_skips_understanding_for_no_slot_intent() -> None:
    async def run() -> None:
        intent = IntentDefinition(
            intent_code="query_account_balance",
            name="查询余额",
            description="查询账户余额",
            agent_url="http://agent/balance",
            slot_schema=[],
        )
        orchestrator = GraphRouterOrchestrator(
            publish_event=lambda event: None,
            intent_catalog=_SingleIntentCatalog(intent),
        )
        orchestrator.understanding_validator.validate_node = AsyncMock(
            side_effect=AssertionError("no-slot intent should not run router slot validation")
        )
        orchestrator._publish_node_state = AsyncMock()

        session = GraphSessionState(session_id="session_no_slot_task", cust_id="cust_no_slot_task")
        graph = ExecutionGraphState(source_message="帮我查余额")
        node = GraphNodeState(
            intent_code=intent.intent_code,
            title="查询余额",
            confidence=0.95,
            status=GraphNodeStatus.READY,
        )
        graph.nodes.append(node)

        task = await orchestrator._create_task_for_node(
            session,
            graph,
            node,
            dispatch_input="帮我查余额",
        )

        assert task is not None
        assert task.intent_code == intent.intent_code
        assert node.task_id == task.task_id
        assert session.last_diagnostics == []

    asyncio.run(run())


def test_graph_orchestrator_router_only_skips_understanding_for_no_slot_intent() -> None:
    async def run() -> None:
        intent = IntentDefinition(
            intent_code="query_account_balance",
            name="查询余额",
            description="查询账户余额",
            agent_url="http://agent/balance",
            slot_schema=[],
        )
        orchestrator = GraphRouterOrchestrator(
            publish_event=lambda event: None,
            intent_catalog=_SingleIntentCatalog(intent),
        )
        orchestrator.understanding_validator.validate_node = AsyncMock(
            side_effect=AssertionError("no-slot intent should not run router slot validation")
        )
        orchestrator._publish_node_state = AsyncMock()
        orchestrator._publish_graph_state = AsyncMock()

        session = GraphSessionState(session_id="session_no_slot_router_only", cust_id="cust_no_slot_router_only")
        graph = ExecutionGraphState(source_message="帮我查余额")
        node = GraphNodeState(
            intent_code=intent.intent_code,
            title="查询余额",
            confidence=0.95,
            status=GraphNodeStatus.READY,
        )
        graph.nodes.append(node)

        ready = await orchestrator._prepare_node_router_only(
            session,
            graph,
            node,
            dispatch_input="帮我查余额",
        )

        assert ready is True
        assert node.status == GraphNodeStatus.READY_FOR_DISPATCH
        assert graph.status == GraphStatus.READY_FOR_DISPATCH
        assert session.last_diagnostics == []

    asyncio.run(run())


def test_graph_orchestrator_router_only_waits_when_semantic_string_slots_are_missing() -> None:
    async def run() -> None:
        intent = IntentDefinition(
            intent_code="AG_TRANS",
            name="转账",
            description="执行转账，需要收款人姓名和金额。",
            agent_url="http://agent/transfer",
            slot_schema=[
                {
                    "slot_key": "payee_name",
                    "field_code": "payee_name",
                    "label": "收款人姓名",
                    "description": "当前转账的收款人姓名",
                    "aliases": ["收款人", "对方姓名"],
                    "value_type": "string",
                    "required": True,
                },
                {
                    "slot_key": "amount",
                    "field_code": "amount",
                    "label": "转账金额",
                    "description": "当前转账金额",
                    "value_type": "currency",
                    "required": True,
                },
                {
                    "slot_key": "payee_card_no",
                    "label": "收款卡号",
                    "aliases": ["收款卡号", "对方卡号"],
                    "value_type": "string",
                    "required": False,
                },
            ],
        )
        orchestrator = GraphRouterOrchestrator(
            publish_event=lambda event: None,
            intent_catalog=_SingleIntentCatalog(intent),
        )
        orchestrator._publish_node_state = AsyncMock()
        orchestrator._publish_graph_state = AsyncMock()

        session = GraphSessionState(session_id="session_transfer_router_only", cust_id="cust_transfer_router_only")
        graph = ExecutionGraphState(source_message="给小明转500元")
        node = GraphNodeState(
            intent_code=intent.intent_code,
            title="转账",
            confidence=0.97,
            status=GraphNodeStatus.READY,
            source_fragment="给小明转500元",
        )
        graph.nodes.append(node)

        ready = await orchestrator._prepare_node_router_only(
            session,
            graph,
            node,
            dispatch_input="给小明转500元",
        )

        assert ready is False
        assert node.status == GraphNodeStatus.WAITING_USER_INPUT
        assert graph.status == GraphStatus.WAITING_USER_INPUT
        assert node.slot_memory == {"amount": "500"}
        assert "收款人姓名" in session.messages[-1].content

    asyncio.run(run())
