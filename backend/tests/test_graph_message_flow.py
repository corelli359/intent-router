from __future__ import annotations

import asyncio
from typing import Any

from router_service.core.graph.message_flow import GraphMessageFlow
from router_service.core.graph.planner import TurnDecisionPayload
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.recognition.understanding_service import TurnInterpretationResult
from router_service.core.graph.session_store import GraphSessionStore
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphNodeState,
    GraphNodeStatus,
    GraphStatus,
)


class DummyGraphCompiler:
    def guided_selection_display_content(self, _):  # pragma: no cover - minimal stub
        return ""

    async def compile_message(self, *args, **kwargs):  # pragma: no cover - should not run
        raise AssertionError("compile_message should not be called in message flow tests")

    async def compile_proactive_interactive_graph(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("compile_proactive_interactive_graph should not be called")

    def guided_selection_from_proactive_items(self, *_args, **_kwargs) -> list[Any]:  # pragma: no cover
        return []

    def build_guided_selection_graph(self, content: str, **_kwargs) -> ExecutionGraphState:
        graph = ExecutionGraphState(source_message=content)
        return graph


class RecordingGraphCompiler(DummyGraphCompiler):
    def __init__(self) -> None:
        self.last_compile_kwargs: dict[str, Any] | None = None

    async def compile_message(self, session, content: str, **kwargs):
        del session, content
        self.last_compile_kwargs = kwargs
        return type(
            "GraphCompilationResult",
            (),
            {
                "recognition": RecognitionResult(primary=[], candidates=[]),
                "diagnostics": [],
                "graph": None,
                "no_match": True,
            },
        )()


class DummyRecommendationRouter:
    async def decide(self, *args, **kwargs) -> Any:  # pragma: no cover
        raise AssertionError("proactive routing is not part of these tests")


class DummyStateSync:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def publish_session_state(self, *_args, **_kwargs) -> None:
        self.calls.append("session_state")

    async def publish_pending_graph(self, *_args, **_kwargs) -> None:
        self.calls.append("pending_graph")

    async def publish_graph_state(self, *_args, **_kwargs) -> None:
        self.calls.append("graph_state")

    async def publish_no_match_hint(self, *_args, **_kwargs) -> None:
        self.calls.append("no_match_hint")

    async def publish_graph_waiting_hint(self, *_args, **_kwargs) -> None:
        self.calls.append("waiting_hint")


class StubUnderstandingService:
    def __init__(self, pending: TurnDecisionPayload | None = None, waiting: TurnDecisionPayload | None = None) -> None:
        self.pending_decision = pending or TurnDecisionPayload(action="wait")
        self.waiting_decision = waiting or TurnDecisionPayload(action="wait")
        self.pending_calls: list[str] = []
        self.waiting_calls: list[str] = []

    async def interpret_pending_graph_turn(self, session, *, content: str, pending_graph):
        self.pending_calls.append(content)
        return TurnInterpretationResult(decision=self.pending_decision, recognition=RecognitionResult(primary=[], candidates=[]))

    async def interpret_waiting_node_turn(self, session, *, content: str, waiting_node, current_graph):
        self.waiting_calls.append(content)
        return TurnInterpretationResult(decision=self.waiting_decision, recognition=RecognitionResult(primary=[], candidates=[]))


class CallbackTracker:
    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def activate_graph(self, graph: ExecutionGraphState) -> None:
        self.actions.append(("activate_graph", graph.graph_id))

    async def drain_graph(self, session, source_message: str) -> None:
        self.actions.append(("drain_graph", source_message))

    async def cancel_pending_graph(self, session, graph_id, confirm_token) -> None:
        self.actions.append(("cancel_pending_graph", graph_id))

    async def cancel_current_graph(self, session, reason: str) -> None:
        self.actions.append(("cancel_current_graph", reason))

    async def confirm_pending_graph(self, session, graph_id, confirm_token) -> None:
        self.actions.append(("confirm_pending_graph", graph_id))

    async def resume_waiting_node(self, session, waiting_node, content: str) -> None:
        self.actions.append(("resume_waiting_node", waiting_node.node_id))

    async def cancel_current_node(self, session, reason: str) -> None:
        self.actions.append(("cancel_current_node", reason))

    async def route_new_message(self, session, content: str, **kwargs) -> None:
        self.actions.append(("route_new_message", content, kwargs))


def waiting_node_selector(session: Any) -> GraphNodeState | None:
    graph = session.current_graph
    if graph is None:
        return None
    for node in graph.nodes:
        if node.status in {GraphNodeStatus.WAITING_USER_INPUT, GraphNodeStatus.WAITING_CONFIRMATION}:
            return node
    return None


def build_message_flow(understanding_service: StubUnderstandingService) -> tuple[GraphMessageFlow, GraphSessionStore, CallbackTracker, DummyStateSync]:
    session_store = GraphSessionStore()
    callbacks = CallbackTracker()
    state_sync = DummyStateSync()
    flow = GraphMessageFlow(
        session_store=session_store,
        graph_compiler=DummyGraphCompiler(),
        understanding_service=understanding_service,
        recommendation_router=DummyRecommendationRouter(),
        state_sync=state_sync,
        get_waiting_node=waiting_node_selector,
        build_session_context=lambda _: {"recent_messages": [], "long_term_memory": []},
        activate_graph=callbacks.activate_graph,
        drain_graph=callbacks.drain_graph,
        cancel_pending_graph=callbacks.cancel_pending_graph,
        cancel_current_graph=callbacks.cancel_current_graph,
        confirm_pending_graph=callbacks.confirm_pending_graph,
        resume_waiting_node=callbacks.resume_waiting_node,
        cancel_current_node=callbacks.cancel_current_node,
    )
    flow.route_new_message = callbacks.route_new_message
    return flow, session_store, callbacks, state_sync


def make_graph(status: GraphStatus) -> ExecutionGraphState:
    return ExecutionGraphState(source_message="source", status=status)


def test_pending_graph_confirm_triggers_confirm_callback() -> None:
    decision = TurnDecisionPayload(action="confirm_pending_graph")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(pending=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    session.pending_graph = make_graph(GraphStatus.WAITING_CONFIRMATION)

    asyncio.run(flow.handle_pending_graph_turn(session, "yes"))

    assert ("confirm_pending_graph", None) in callbacks.actions


def test_pending_graph_wait_triggers_hint() -> None:
    flow, store, _, state_sync = build_message_flow(StubUnderstandingService())
    session = store.create(cust_id="cust", session_id="session-id")
    session.pending_graph = make_graph(GraphStatus.WAITING_CONFIRMATION)

    asyncio.run(flow.handle_pending_graph_turn(session, ""))

    assert "waiting_hint" in state_sync.calls


def test_pending_graph_replan_routes_new_message() -> None:
    decision = TurnDecisionPayload(action="replan")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(pending=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    session.pending_graph = make_graph(GraphStatus.WAITING_CONFIRMATION)

    asyncio.run(flow.handle_pending_graph_turn(session, "reroute"))

    assert session.pending_graph is None
    assert session.workflow.suspended_business_ids
    assert any(call[0] == "route_new_message" for call in callbacks.actions)


def test_pending_graph_replan_preserves_emit_events_flag() -> None:
    decision = TurnDecisionPayload(action="replan")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(pending=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    session.pending_graph = make_graph(GraphStatus.WAITING_CONFIRMATION)

    asyncio.run(flow.handle_pending_graph_turn(session, "reroute", emit_events=True))

    route_calls = [call for call in callbacks.actions if call[0] == "route_new_message"]
    assert route_calls
    assert route_calls[-1][2]["emit_events"] is True


def test_pending_graph_cancel_triggers_cancel_callback() -> None:
    decision = TurnDecisionPayload(action="cancel_pending_graph")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(pending=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    session.pending_graph = make_graph(GraphStatus.WAITING_CONFIRMATION)

    asyncio.run(flow.handle_pending_graph_turn(session, "no"))

    assert ("cancel_pending_graph", None) in callbacks.actions


def test_waiting_node_resume_invokes_resume_callback() -> None:
    decision = TurnDecisionPayload(action="resume_current")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(waiting=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent",
        title="wait",
        confidence=0.5,
        status=GraphNodeStatus.WAITING_USER_INPUT,
    )
    graph.nodes.append(node)
    session.current_graph = graph

    asyncio.run(flow.handle_waiting_node_turn(session, node, "resume"))

    assert ("resume_waiting_node", node.node_id) in callbacks.actions


def test_waiting_node_cancel_calls_cancel_and_drains() -> None:
    decision = TurnDecisionPayload(action="cancel_current")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(waiting=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent",
        title="wait",
        confidence=0.5,
        status=GraphNodeStatus.WAITING_USER_INPUT,
    )
    graph.nodes.append(node)
    session.current_graph = graph

    asyncio.run(flow.handle_waiting_node_turn(session, node, "cancel"))

    assert ("cancel_current_node", "cancel_current") in callbacks.actions
    assert ("drain_graph", "cancel") in callbacks.actions


def test_waiting_node_replan_suspends_and_rebuilds() -> None:
    decision = TurnDecisionPayload(action="replan")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(waiting=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent",
        title="wait",
        confidence=0.5,
        status=GraphNodeStatus.WAITING_USER_INPUT,
    )
    graph.nodes.append(node)
    session.current_graph = graph

    asyncio.run(flow.handle_waiting_node_turn(session, node, "replan"))

    assert session.current_graph is None
    assert session.workflow.suspended_business_ids
    assert any(call[0] == "route_new_message" for call in callbacks.actions)


def test_waiting_node_replan_preserves_emit_events_flag() -> None:
    decision = TurnDecisionPayload(action="replan")
    flow, store, callbacks, _ = build_message_flow(StubUnderstandingService(waiting=decision))
    session = store.create(cust_id="cust", session_id="session-id")
    graph = make_graph(GraphStatus.RUNNING)
    node = GraphNodeState(
        intent_code="intent",
        title="wait",
        confidence=0.5,
        status=GraphNodeStatus.WAITING_USER_INPUT,
    )
    graph.nodes.append(node)
    session.current_graph = graph

    asyncio.run(flow.handle_waiting_node_turn(session, node, "replan", emit_events=True))

    route_calls = [call for call in callbacks.actions if call[0] == "route_new_message"]
    assert route_calls
    assert route_calls[-1][2]["emit_events"] is True


def test_route_new_message_forwards_emit_events_to_graph_compiler() -> None:
    compiler = RecordingGraphCompiler()
    understanding_service = StubUnderstandingService()
    session_store = GraphSessionStore()
    state_sync = DummyStateSync()
    callbacks = CallbackTracker()
    flow = GraphMessageFlow(
        session_store=session_store,
        graph_compiler=compiler,
        understanding_service=understanding_service,
        recommendation_router=DummyRecommendationRouter(),
        state_sync=state_sync,
        get_waiting_node=waiting_node_selector,
        build_session_context=lambda _: {"recent_messages": [], "long_term_memory": []},
        activate_graph=callbacks.activate_graph,
        drain_graph=callbacks.drain_graph,
        cancel_pending_graph=callbacks.cancel_pending_graph,
        cancel_current_graph=callbacks.cancel_current_graph,
        confirm_pending_graph=callbacks.confirm_pending_graph,
        resume_waiting_node=callbacks.resume_waiting_node,
        cancel_current_node=callbacks.cancel_current_node,
    )
    session = session_store.create(cust_id="cust", session_id="session-id")

    asyncio.run(flow.route_new_message(session, "给王芳转 100 元", emit_events=True))

    assert compiler.last_compile_kwargs is not None
    assert compiler.last_compile_kwargs["emit_events"] is True
