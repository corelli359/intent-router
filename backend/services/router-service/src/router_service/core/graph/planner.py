from __future__ import annotations
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from router_service.core.shared.domain import IntentDefinition, IntentMatch
from router_service.core.shared.diagnostics import (
    RouterDiagnosticCode,
    diagnostic,
    merge_diagnostics,
)
from router_service.core.support.json_codec import json_dumps
from router_service.core.support.llm_client import JsonLLMClient
from router_service.core.prompts.prompt_templates import (
    DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT,
    DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT,
    DEFAULT_TURN_INTERPRETER_HUMAN_PROMPT,
    DEFAULT_TURN_INTERPRETER_SYSTEM_PROMPT,
    build_graph_planner_prompt,
    build_turn_interpreter_prompt,
)
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphStatus,
)


_PLANNER_INTENT_DEFINITION_CACHE_LIMIT = 2048
_planner_intent_definition_cache: dict[int, tuple[IntentDefinition, dict[str, Any]]] = {}


def planner_intent_definition_payload(intent: IntentDefinition) -> dict[str, Any]:
    """Build and cache the planner-side static definition payload for one intent."""
    cached = _planner_intent_definition_cache.get(id(intent))
    if cached is not None and cached[0] is intent:
        return cached[1]
    payload = {
        "name": intent.name,
        "description": intent.description,
        "examples": intent.examples,
        "field_catalog": [field.model_dump(mode="json") for field in intent.field_catalog],
        "slot_schema": [slot.model_dump(mode="json") for slot in intent.slot_schema],
        "graph_build_hints": intent.graph_build_hints.model_dump(mode="json"),
    }
    if len(_planner_intent_definition_cache) >= _PLANNER_INTENT_DEFINITION_CACHE_LIMIT:
        _planner_intent_definition_cache.clear()
    _planner_intent_definition_cache[id(intent)] = (intent, payload)
    return payload


class GraphPlanConditionPayload(BaseModel):
    """LLM planner representation of a runtime edge condition."""

    expected_statuses: list[str] = Field(default_factory=lambda: ["completed"])
    left_key: str | None = None
    operator: Literal[">", ">=", "==", "<", "<="] | None = None
    right_value: float | int | str | bool | None = None


class GraphPlanNodePayload(BaseModel):
    """LLM planner representation of one graph node."""

    intent_code: str
    title: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_fragment: str | None = None
    slot_memory: dict[str, Any] = Field(default_factory=dict)


class GraphPlanEdgePayload(BaseModel):
    """LLM planner representation of one graph edge."""

    source_index: int = Field(ge=0)
    target_index: int = Field(ge=0)
    relation_type: GraphEdgeType = GraphEdgeType.SEQUENTIAL
    label: str | None = None
    condition: GraphPlanConditionPayload | None = None


class GraphPlanningPayload(BaseModel):
    """Structured output expected from the graph planner model."""

    summary: str = ""
    needs_confirmation: bool = True
    nodes: list[GraphPlanNodePayload] = Field(default_factory=list)
    edges: list[GraphPlanEdgePayload] = Field(default_factory=list)


class GraphPlanNormalizer:
    """Normalizes planner output into executable graph domain models."""

    def normalize(
        self,
        *,
        payload: GraphPlanningPayload,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
    ) -> ExecutionGraphState:
        """Filter invalid planner output and rebuild stable runtime graph objects."""
        confidence_by_code: dict[str, float] = {}
        for match in matches:
            confidence_by_code[match.intent_code] = max(confidence_by_code.get(match.intent_code, 0.0), match.confidence)

        allowed_intents = {match.intent_code for match in matches}
        graph = ExecutionGraphState(
            source_message=message,
            summary=payload.summary,
            status=GraphStatus.WAITING_CONFIRMATION if payload.needs_confirmation or len(payload.nodes) > 1 else GraphStatus.DRAFT,
            actions=[] if not (payload.needs_confirmation or len(payload.nodes) > 1) else [
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ],
        )

        for index, node_payload in enumerate(payload.nodes):
            if node_payload.intent_code not in allowed_intents:
                continue
            intent = intents_by_code.get(node_payload.intent_code)
            if intent is None:
                continue
            graph.nodes.append(
                GraphNodeState(
                    intent_code=node_payload.intent_code,
                    title=node_payload.title or intent.name,
                    confidence=(
                        node_payload.confidence
                        if node_payload.confidence is not None
                        else confidence_by_code.get(node_payload.intent_code, 0.0)
                    ),
                    position=index,
                    source_fragment=node_payload.source_fragment or message,
                    slot_memory=dict(node_payload.slot_memory),
                )
            )

        for edge_payload in payload.edges:
            if edge_payload.source_index >= len(graph.nodes) or edge_payload.target_index >= len(graph.nodes):
                continue
            source = graph.nodes[edge_payload.source_index]
            target = graph.nodes[edge_payload.target_index]
            if source.node_id not in target.depends_on:
                target.depends_on.append(source.node_id)
            target.relation_reason = edge_payload.label
            graph.edges.append(
                GraphEdge(
                    source_node_id=source.node_id,
                    target_node_id=target.node_id,
                    relation_type=edge_payload.relation_type,
                    label=edge_payload.label,
                    condition=(
                        GraphCondition(
                            source_node_id=source.node_id,
                            expected_statuses=edge_payload.condition.expected_statuses,
                            left_key=edge_payload.condition.left_key,
                            operator=edge_payload.condition.operator,
                            right_value=edge_payload.condition.right_value,
                        )
                        if edge_payload.condition is not None
                        else None
                    ),
                )
            )

        if not graph.summary:
            graph.summary = (
                f"识别到 {len(graph.nodes)} 个事项，已生成动态执行图"
                if len(graph.nodes) > 1
                else f"识别到事项：{graph.nodes[0].title}" if graph.nodes else "未识别到明确事项"
            )
        return graph


class TurnDecisionPayload(BaseModel):
    """Structured turn interpretation result for pending graph or waiting node."""

    action: Literal[
        "resume_current",
        "cancel_current",
        "replan",
        "confirm_pending_graph",
        "cancel_pending_graph",
        "wait",
    ]
    reason: str = ""
    target_intent_code: str | None = None

    @model_validator(mode="after")
    def normalize_reason(self) -> "TurnDecisionPayload":
        """Ensure the decision always carries a non-empty explanatory reason."""
        self.reason = self.reason.strip() or self.action
        return self


class IntentGraphPlanner(Protocol):
    """Protocol for components that can turn recognized intents into an execution graph."""

    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> ExecutionGraphState:
        """Plan an execution graph from recognized intent matches."""
        ...


class TurnInterpreter(Protocol):
    """Protocol for components that interpret follow-up turns while graph execution is blocked."""

    async def interpret_pending_graph(
        self,
        *,
        message: str,
        pending_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
        """Interpret a turn while the router is waiting on graph confirmation."""
        ...

    async def interpret_waiting_node(
        self,
        *,
        message: str,
        waiting_node: GraphNodeState,
        current_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
        """Interpret a turn while one node is waiting for more user input."""
        ...


class SequentialIntentGraphPlanner:
    """Deterministic fallback planner that executes matched intents sequentially."""

    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
        recommend_task: list[dict[str, Any]] | None = None,
    ) -> ExecutionGraphState:
        """Build a simple sequential graph in the order of recognized matches."""
        graph = ExecutionGraphState(
            source_message=message,
            status=GraphStatus.WAITING_CONFIRMATION if len(matches) > 1 else GraphStatus.DRAFT,
            actions=self._default_actions(len(matches)),
        )
        for index, match in enumerate(matches):
            intent = intents_by_code.get(match.intent_code)
            if intent is None:
                continue
            graph.nodes.append(
                GraphNodeState(
                    intent_code=intent.intent_code,
                    title=intent.name,
                    confidence=match.confidence,
                    position=index,
                    source_fragment=message,
                )
            )
        for index in range(1, len(graph.nodes)):
            previous = graph.nodes[index - 1]
            current = graph.nodes[index]
            current.depends_on.append(previous.node_id)
            current.relation_reason = "按识别顺序依次执行"
            graph.edges.append(
                GraphEdge(
                    source_node_id=previous.node_id,
                    target_node_id=current.node_id,
                    relation_type=GraphEdgeType.SEQUENTIAL,
                    label="按识别顺序执行",
                )
            )
        graph.summary = (
            f"识别到 {len(graph.nodes)} 个事项，已按识别顺序生成执行图"
            if len(graph.nodes) > 1
            else f"识别到事项：{graph.nodes[0].title}" if graph.nodes else "未识别到明确事项"
        )
        return graph

    def _default_actions(self, node_count: int) -> list[GraphAction]:
        """Return the default graph-card actions for the given node count."""
        if node_count <= 1:
            return []
        return [
            GraphAction(code="confirm_graph", label="开始执行"),
            GraphAction(code="cancel_graph", label="取消"),
        ]


class BasicTurnInterpreter:
    """Rule-based fallback interpreter for pending graphs and waiting nodes."""

    async def interpret_pending_graph(
        self,
        *,
        message: str,
        pending_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
        """Replan on new primary intent, otherwise keep waiting for an explicit action."""
        if recognition.primary:
            return TurnDecisionPayload(
                action="replan",
                reason="识别到新的主意图，建议重新规划",
                target_intent_code=recognition.primary[0].intent_code,
            )
        return TurnDecisionPayload(action="wait", reason="等待显式按钮操作")

    async def interpret_waiting_node(
        self,
        *,
        message: str,
        waiting_node: GraphNodeState,
        current_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
        """Replan on intent switch, otherwise resume the current waiting node."""
        different_match = next(
            (match for match in recognition.primary if match.intent_code != waiting_node.intent_code),
            None,
        )
        if different_match is not None:
            return TurnDecisionPayload(
                action="replan",
                reason=f"识别到新的主意图 {different_match.intent_code}",
                target_intent_code=different_match.intent_code,
            )
        return TurnDecisionPayload(action="resume_current", reason="继续补充当前节点")


class LLMIntentGraphPlanner:
    """LLM-backed planner that turns matched intents into a graph plan."""

    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: IntentGraphPlanner | None = None,
        system_prompt_template: str = DEFAULT_GRAPH_PLANNER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_GRAPH_PLANNER_HUMAN_PROMPT,
    ) -> None:
        """Initialize the planner with an LLM client, fallback planner, and prompt."""
        self.llm_client = llm_client
        self.model = model
        self.fallback = fallback or SequentialIntentGraphPlanner()
        self.normalizer = GraphPlanNormalizer()
        self.prompt = build_graph_planner_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def _fallback_plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None,
        long_term_memory: list[str] | None,
        recommend_task: list[dict[str, Any]] | None = None,
        diagnostic_message: str,
        diagnostic_details: dict[str, Any],
    ) -> ExecutionGraphState:
        """Run the fallback planner and attach one planner diagnostic."""
        graph = await self.fallback.plan(
            message=message,
            matches=matches,
            intents_by_code=intents_by_code,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            recommend_task=recommend_task,
        )
        graph.diagnostics = merge_diagnostics(
            graph.diagnostics,
            [
                diagnostic(
                    RouterDiagnosticCode.GRAPH_PLANNER_LLM_FAILED_FALLBACK,
                    source="planner",
                    message=diagnostic_message,
                    details=diagnostic_details,
                )
            ],
        )
        return graph

    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
        recommend_task: list[dict[str, Any]] | None = None,
    ) -> ExecutionGraphState:
        """Plan an execution graph from recognized intents, with deterministic fallback."""
        if not matches:
            return await self.fallback.plan(
                message=message,
                matches=matches,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )

        try:
            raw_payload = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "message": message,
                    "recommend_task_json": json_dumps(recommend_task or []),
                    "recent_messages_json": json_dumps(recent_messages or []),
                    "long_term_memory_json": json_dumps(long_term_memory or []),
                    "matched_intents_json": json_dumps(
                        [
                            {
                                "intent_code": match.intent_code,
                                "confidence": match.confidence,
                                "reason": match.reason,
                                "definition": planner_intent_definition_payload(intents_by_code[match.intent_code]),
                            }
                            for match in matches
                            if match.intent_code in intents_by_code
                        ]
                    ),
                },
                model=self.model,
            )
            payload = GraphPlanningPayload.model_validate(raw_payload)
        except Exception as exc:
            return await self._fallback_plan(
                message=message,
                matches=matches,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                recommend_task=recommend_task,
                diagnostic_message="图规划 LLM 失败，已降级到顺序规划器",
                diagnostic_details={
                    "fallback": type(self.fallback).__name__,
                    "error_type": type(exc).__name__,
                },
            )

        graph = self.normalizer.normalize(
            payload=payload,
            message=message,
            matches=matches,
            intents_by_code=intents_by_code,
        )
        if not graph.nodes:
            fallback_graph = await self.fallback.plan(
                message=message,
                matches=matches,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                recommend_task=recommend_task,
            )
            fallback_graph.diagnostics = merge_diagnostics(
                fallback_graph.diagnostics,
                [
                    diagnostic(
                        RouterDiagnosticCode.GRAPH_PLANNER_EMPTY_GRAPH_FALLBACK,
                        source="planner",
                        message="图规划未产出节点，已降级到顺序规划器",
                        details={"fallback": type(self.fallback).__name__},
                    )
                ],
            )
            return fallback_graph
        return graph

class LLMGraphTurnInterpreter:
    """LLM-backed interpreter for blocked graph turns."""

    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: TurnInterpreter | None = None,
        system_prompt_template: str = DEFAULT_TURN_INTERPRETER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_TURN_INTERPRETER_HUMAN_PROMPT,
    ) -> None:
        """Initialize the turn interpreter with an LLM client and fallback interpreter."""
        self.llm_client = llm_client
        self.model = model
        self.fallback = fallback or BasicTurnInterpreter()
        self.prompt = build_turn_interpreter_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def interpret_pending_graph(
        self,
        *,
        message: str,
        pending_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
        """Interpret a user turn while the router is waiting on graph confirmation."""
        return await self._interpret(
            mode="pending_graph",
            message=message,
            waiting_node_json="null",
            current_graph_json="null",
            pending_graph_json=json_dumps(pending_graph.model_dump(mode="json")),
            recognition=recognition,
            fallback=lambda: self.fallback.interpret_pending_graph(
                message=message,
                pending_graph=pending_graph,
                recognition=recognition,
            ),
        )

    async def interpret_waiting_node(
        self,
        *,
        message: str,
        waiting_node: GraphNodeState,
        current_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
        """Interpret a user turn while one node is waiting on additional user input."""
        return await self._interpret(
            mode="waiting_node",
            message=message,
            waiting_node_json=json_dumps(waiting_node.model_dump(mode="json")),
            current_graph_json=json_dumps(current_graph.model_dump(mode="json")),
            pending_graph_json="null",
            recognition=recognition,
            fallback=lambda: self.fallback.interpret_waiting_node(
                message=message,
                waiting_node=waiting_node,
                current_graph=current_graph,
                recognition=recognition,
            ),
        )

    async def _interpret(
        self,
        *,
        mode: str,
        message: str,
        waiting_node_json: str,
        current_graph_json: str,
        pending_graph_json: str,
        recognition: RecognitionResult,
        fallback,
    ) -> TurnDecisionPayload:
        """Run the LLM interpreter and degrade to the fallback interpreter on failure."""
        try:
            raw_payload = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "mode": mode,
                    "message": message,
                    "waiting_node_json": waiting_node_json,
                    "current_graph_json": current_graph_json,
                    "pending_graph_json": pending_graph_json,
                    "primary_intents_json": json_dumps([match.model_dump(mode="json") for match in recognition.primary]),
                    "candidate_intents_json": json_dumps(
                        [match.model_dump(mode="json") for match in recognition.candidates]
                    ),
                },
                model=self.model,
            )
            return TurnDecisionPayload.model_validate(raw_payload)
        except Exception:
            return await fallback()
