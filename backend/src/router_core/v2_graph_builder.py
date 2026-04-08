from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, Field

from models.intent import GraphConfirmPolicy
from router_core.domain import IntentDefinition, IntentMatch
from router_core.llm_client import AsyncDeltaCallback, JsonLLMClient
from router_core.prompt_templates import (
    DEFAULT_V2_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT,
    DEFAULT_V2_UNIFIED_GRAPH_BUILDER_SYSTEM_PROMPT,
    build_v2_unified_graph_builder_prompt,
)
from router_core.recognizer import IntentRecognizer, NullIntentRecognizer, RecognitionResult, recognition_intent_payload
from router_core.v2_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphStatus,
)
from router_core.v2_planner import IntentGraphPlanner, SequentialIntentGraphPlanner


logger = logging.getLogger(__name__)


class GraphDraftIntentPayload(BaseModel):
    intent_code: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = "llm returned a match"


class GraphDraftConditionPayload(BaseModel):
    expected_statuses: list[str] = Field(default_factory=lambda: ["completed"])
    left_key: str | None = None
    operator: Literal[">", ">=", "==", "<", "<="] | None = None
    right_value: float | int | str | bool | None = None


class GraphDraftNodePayload(BaseModel):
    intent_code: str
    title: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_fragment: str | None = None
    slot_memory: dict[str, Any] = Field(default_factory=dict)


class GraphDraftEdgePayload(BaseModel):
    source_index: int = Field(ge=0)
    target_index: int = Field(ge=0)
    relation_type: GraphEdgeType = GraphEdgeType.SEQUENTIAL
    label: str | None = None
    condition: GraphDraftConditionPayload | None = None


class UnifiedGraphDraftPayload(BaseModel):
    summary: str = ""
    needs_confirmation: bool = False
    primary_intents: list[GraphDraftIntentPayload] = Field(default_factory=list)
    candidate_intents: list[GraphDraftIntentPayload] = Field(default_factory=list)
    nodes: list[GraphDraftNodePayload] = Field(default_factory=list)
    edges: list[GraphDraftEdgePayload] = Field(default_factory=list)


@dataclass(slots=True)
class GraphBuildResult:
    recognition: RecognitionResult
    graph: ExecutionGraphState


class IntentGraphBuilder(Protocol):
    async def build(
        self,
        *,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        recognition: RecognitionResult | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> GraphBuildResult: ...


class GraphDraftNormalizer:
    def normalize(
        self,
        *,
        payload: UnifiedGraphDraftPayload,
        message: str,
        intents_by_code: dict[str, IntentDefinition],
    ) -> GraphBuildResult:
        recognition = self._normalize_recognition(payload=payload, intents_by_code=intents_by_code)
        graph = self._normalize_graph(
            payload=payload,
            message=message,
            recognition=recognition,
            intents_by_code=intents_by_code,
        )
        return GraphBuildResult(recognition=recognition, graph=graph)

    def _normalize_recognition(
        self,
        *,
        payload: UnifiedGraphDraftPayload,
        intents_by_code: dict[str, IntentDefinition],
    ) -> RecognitionResult:
        primary: list[IntentMatch] = []
        candidates: list[IntentMatch] = []
        seen_codes: set[str] = set()

        for item in payload.primary_intents:
            intent = intents_by_code.get(item.intent_code)
            if intent is None or item.intent_code in seen_codes:
                continue
            confidence = round(min(0.99, max(0.0, float(item.confidence))), 2)
            if confidence >= intent.primary_threshold:
                primary.append(
                    IntentMatch(intent_code=item.intent_code, confidence=confidence, reason=item.reason or "unified_builder")
                )
                seen_codes.add(item.intent_code)
                continue
            if confidence >= intent.candidate_threshold:
                candidates.append(
                    IntentMatch(intent_code=item.intent_code, confidence=confidence, reason=item.reason or "unified_builder")
                )
                seen_codes.add(item.intent_code)

        for item in payload.candidate_intents:
            intent = intents_by_code.get(item.intent_code)
            if intent is None or item.intent_code in seen_codes:
                continue
            confidence = round(min(0.99, max(0.0, float(item.confidence))), 2)
            if confidence < intent.candidate_threshold:
                continue
            candidates.append(
                IntentMatch(intent_code=item.intent_code, confidence=confidence, reason=item.reason or "unified_builder")
            )
            seen_codes.add(item.intent_code)

        def _sort_key(match: IntentMatch) -> tuple[int, float]:
            intent = intents_by_code[match.intent_code]
            return (intent.dispatch_priority, match.confidence)

        primary.sort(key=_sort_key, reverse=True)
        candidates.sort(key=_sort_key, reverse=True)
        return RecognitionResult(primary=primary, candidates=candidates)

    def _normalize_graph(
        self,
        *,
        payload: UnifiedGraphDraftPayload,
        message: str,
        recognition: RecognitionResult,
        intents_by_code: dict[str, IntentDefinition],
    ) -> ExecutionGraphState:
        confidence_by_code = {match.intent_code: match.confidence for match in recognition.primary}
        allowed_intents = set(confidence_by_code)

        graph = ExecutionGraphState(
            source_message=message,
            summary=payload.summary,
            status=GraphStatus.DRAFT,
            actions=[],
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

        needs_confirmation = self._resolve_confirmation_needed(
            payload=payload,
            graph=graph,
            intents_by_code=intents_by_code,
        )
        graph.status = GraphStatus.WAITING_CONFIRMATION if needs_confirmation else GraphStatus.DRAFT
        graph.actions = (
            [
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ]
            if needs_confirmation
            else []
        )
        if not graph.summary:
            graph.summary = (
                f"识别到 {len(graph.nodes)} 个事项，已生成执行图"
                if len(graph.nodes) > 1
                else f"识别到事项：{graph.nodes[0].title}" if graph.nodes else "未识别到明确事项"
            )
        return graph

    def _resolve_confirmation_needed(
        self,
        *,
        payload: UnifiedGraphDraftPayload,
        graph: ExecutionGraphState,
        intents_by_code: dict[str, IntentDefinition],
    ) -> bool:
        if not graph.nodes:
            return False
        confirm_policies = {
            intents_by_code[node.intent_code].graph_build_hints.confirm_policy
            for node in graph.nodes
            if node.intent_code in intents_by_code
        }
        if GraphConfirmPolicy.ALWAYS in confirm_policies:
            return True
        if len(graph.nodes) > 1 and GraphConfirmPolicy.MULTI_NODE_ONLY in confirm_policies:
            return True
        if confirm_policies == {GraphConfirmPolicy.NEVER}:
            return False
        return payload.needs_confirmation or len(graph.nodes) > 1


class LLMIntentGraphBuilder:
    def __init__(
        self,
        llm_client: JsonLLMClient,
        *,
        model: str | None = None,
        fallback_recognizer: IntentRecognizer | None = None,
        fallback_planner: IntentGraphPlanner | None = None,
        normalizer: GraphDraftNormalizer | None = None,
        system_prompt_template: str = DEFAULT_V2_UNIFIED_GRAPH_BUILDER_SYSTEM_PROMPT,
        human_prompt_template: str = DEFAULT_V2_UNIFIED_GRAPH_BUILDER_HUMAN_PROMPT,
    ) -> None:
        self.llm_client = llm_client
        self.model = model
        self.fallback_recognizer = fallback_recognizer or NullIntentRecognizer()
        self.fallback_planner = fallback_planner or SequentialIntentGraphPlanner()
        self.normalizer = normalizer or GraphDraftNormalizer()
        self.prompt = build_v2_unified_graph_builder_prompt(
            system_prompt=system_prompt_template,
            human_prompt=human_prompt_template,
        )

    async def build(
        self,
        *,
        message: str,
        intents: Iterable[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        recognition: RecognitionResult | None = None,
        on_delta: AsyncDeltaCallback | None = None,
    ) -> GraphBuildResult:
        active_intents = [intent for intent in intents if intent.status == "active"]
        if not active_intents:
            return GraphBuildResult(
                recognition=RecognitionResult(primary=[], candidates=[]),
                graph=ExecutionGraphState(source_message=message),
            )

        intents_by_code = {intent.intent_code: intent for intent in active_intents}
        try:
            raw_payload = await self.llm_client.run_json(
                prompt=self.prompt,
                variables={
                    "message": message,
                    "recent_messages_json": json.dumps(recent_messages, ensure_ascii=False, indent=2),
                    "long_term_memory_json": json.dumps(long_term_memory, ensure_ascii=False, indent=2),
                    "recognition_hint_json": json.dumps(
                        {
                            "primary": [match.model_dump(mode="json") for match in (recognition.primary if recognition else [])],
                            "candidates": [
                                match.model_dump(mode="json") for match in (recognition.candidates if recognition else [])
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "intents_json": json.dumps(
                        [recognition_intent_payload(intent) for intent in active_intents],
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
                model=self.model,
                on_delta=on_delta,
            )
            payload = UnifiedGraphDraftPayload.model_validate(raw_payload)
        except Exception:
            logger.warning("Unified graph builder failed, degrading to legacy recognize+plan flow", exc_info=True)
            return await self._build_via_legacy_chain(
                message=message,
                intents=active_intents,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
                recognition=recognition,
                on_delta=on_delta,
            )

        result = self.normalizer.normalize(
            payload=payload,
            message=message,
            intents_by_code=intents_by_code,
        )
        if result.recognition.primary and not result.graph.nodes:
            fallback_graph = await self.fallback_planner.plan(
                message=message,
                matches=result.recognition.primary,
                intents_by_code=intents_by_code,
                recent_messages=recent_messages,
                long_term_memory=long_term_memory,
            )
            return GraphBuildResult(recognition=result.recognition, graph=fallback_graph)
        return result

    async def _build_via_legacy_chain(
        self,
        *,
        message: str,
        intents: list[IntentDefinition],
        recent_messages: list[str],
        long_term_memory: list[str],
        recognition: RecognitionResult | None,
        on_delta: AsyncDeltaCallback | None,
    ) -> GraphBuildResult:
        resolved_recognition = recognition or await self.fallback_recognizer.recognize(
            message=message,
            intents=intents,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            on_delta=on_delta,
        )
        intents_by_code = {intent.intent_code: intent for intent in intents}
        matches = [match for match in resolved_recognition.primary if match.intent_code in intents_by_code]
        graph = await self.fallback_planner.plan(
            message=message,
            matches=matches,
            intents_by_code=intents_by_code,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
        )
        return GraphBuildResult(recognition=resolved_recognition, graph=graph)
