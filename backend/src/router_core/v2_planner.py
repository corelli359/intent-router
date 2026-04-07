from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from router_core.domain import IntentDefinition, IntentMatch
from router_core.llm_client import JsonLLMClient
from router_core.prompt_templates import (
    DEFAULT_V2_GRAPH_PLANNER_HUMAN_PROMPT,
    DEFAULT_V2_GRAPH_PLANNER_SYSTEM_PROMPT,
    DEFAULT_V2_TURN_INTERPRETER_HUMAN_PROMPT,
    DEFAULT_V2_TURN_INTERPRETER_SYSTEM_PROMPT,
    build_v2_graph_planner_prompt,
    build_v2_turn_interpreter_prompt,
)
from router_core.recognizer import RecognitionResult
from router_core.v2_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphStatus,
)


class GraphPlanConditionPayload(BaseModel):
    expected_statuses: list[str] = Field(default_factory=lambda: ["completed"])
    left_key: str | None = None
    operator: Literal[">", ">=", "==", "<", "<="] | None = None
    right_value: float | int | str | bool | None = None
    expression: str | None = None


class GraphPlanNodePayload(BaseModel):
    intent_code: str
    title: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_fragment: str | None = None
    slot_memory: dict[str, Any] = Field(default_factory=dict)


class GraphPlanEdgePayload(BaseModel):
    source_index: int = Field(ge=0)
    target_index: int = Field(ge=0)
    relation_type: GraphEdgeType = GraphEdgeType.SEQUENTIAL
    label: str | None = None
    condition: GraphPlanConditionPayload | None = None


class GraphPlanningPayload(BaseModel):
    summary: str = ""
    needs_confirmation: bool = True
    nodes: list[GraphPlanNodePayload] = Field(default_factory=list)
    edges: list[GraphPlanEdgePayload] = Field(default_factory=list)


class TurnDecisionPayload(BaseModel):
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
        self.reason = self.reason.strip() or self.action
        return self


class IntentGraphPlanner(Protocol):
    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> ExecutionGraphState: ...


class TurnInterpreter(Protocol):
    async def interpret_pending_graph(
        self,
        *,
        message: str,
        pending_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload: ...

    async def interpret_waiting_node(
        self,
        *,
        message: str,
        waiting_node: GraphNodeState,
        current_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload: ...


class SequentialIntentGraphPlanner:
    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> ExecutionGraphState:
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
        if node_count <= 1:
            return []
        return [
            GraphAction(code="confirm_graph", label="开始执行"),
            GraphAction(code="cancel_graph", label="取消"),
        ]


class BasicTurnInterpreter:
    async def interpret_pending_graph(
        self,
        *,
        message: str,
        pending_graph: ExecutionGraphState,
        recognition: RecognitionResult,
    ) -> TurnDecisionPayload:
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
    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: IntentGraphPlanner | None = None,
        system_prompt_template: str = DEFAULT_V2_GRAPH_PLANNER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_V2_GRAPH_PLANNER_HUMAN_PROMPT,
    ) -> None:
        self.llm_client = llm_client
        self.model = model
        self.fallback = fallback or SequentialIntentGraphPlanner()
        self.prompt = build_v2_graph_planner_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
        recent_messages: list[str] | None = None,
        long_term_memory: list[str] | None = None,
    ) -> ExecutionGraphState:
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
                    "recent_messages_json": json.dumps(recent_messages or [], ensure_ascii=False, indent=2),
                    "long_term_memory_json": json.dumps(long_term_memory or [], ensure_ascii=False, indent=2),
                    "matched_intents_json": json.dumps(
                        [
                            {
                                "intent_code": match.intent_code,
                                "confidence": match.confidence,
                                "reason": match.reason,
                                "definition": {
                                    "name": intents_by_code[match.intent_code].name,
                                    "description": intents_by_code[match.intent_code].description,
                                    "examples": intents_by_code[match.intent_code].examples,
                                },
                            }
                            for match in matches
                            if match.intent_code in intents_by_code
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
                model=self.model,
            )
            payload = GraphPlanningPayload.model_validate(raw_payload)
        except Exception:
            return await self.fallback.plan(
                message=message,
                matches=matches,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )

        graph = self._to_graph(
            payload=payload,
            message=message,
            matches=matches,
            intents_by_code=intents_by_code,
        )
        if not graph.nodes:
            return await self.fallback.plan(
                message=message,
                matches=matches,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )
        return graph

    def _to_graph(
        self,
        *,
        payload: GraphPlanningPayload,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
    ) -> ExecutionGraphState:
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
                    confidence=node_payload.confidence if node_payload.confidence is not None else confidence_by_code.get(node_payload.intent_code, 0.0),
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
                            expression=edge_payload.condition.expression,
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


class LLMGraphTurnInterpreter:
    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback: TurnInterpreter | None = None,
        system_prompt_template: str = DEFAULT_V2_TURN_INTERPRETER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_V2_TURN_INTERPRETER_HUMAN_PROMPT,
    ) -> None:
        self.llm_client = llm_client
        self.model = model
        self.fallback = fallback or BasicTurnInterpreter()
        self.prompt = build_v2_turn_interpreter_prompt(
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
        return await self._interpret(
            mode="pending_graph",
            message=message,
            waiting_node_json="null",
            current_graph_json="null",
            pending_graph_json=json.dumps(pending_graph.model_dump(mode="json"), ensure_ascii=False, indent=2),
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
        return await self._interpret(
            mode="waiting_node",
            message=message,
            waiting_node_json=json.dumps(waiting_node.model_dump(mode="json"), ensure_ascii=False, indent=2),
            current_graph_json=json.dumps(current_graph.model_dump(mode="json"), ensure_ascii=False, indent=2),
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
        try:
            raw_payload = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "mode": mode,
                    "message": message,
                    "waiting_node_json": waiting_node_json,
                    "current_graph_json": current_graph_json,
                    "pending_graph_json": pending_graph_json,
                    "primary_intents_json": json.dumps(
                        [match.model_dump(mode="json") for match in recognition.primary],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "candidate_intents_json": json.dumps(
                        [match.model_dump(mode="json") for match in recognition.candidates],
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
                model=self.model,
            )
            return TurnDecisionPayload.model_validate(raw_payload)
        except Exception:
            return await fallback()
