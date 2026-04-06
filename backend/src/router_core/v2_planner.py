from __future__ import annotations

from dataclasses import dataclass

from router_core.domain import IntentDefinition, IntentMatch
from router_core.recognizer import extract_patterns
from router_core.v2_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphStatus,
)


SEQUENCE_CUES = ("先", "然后", "再", "接着", "随后", "之后")
CONDITIONAL_CUES = ("如果", "若", "要是", "成功后", "失败就", "否则", "不够")


@dataclass(slots=True)
class _PlannedIntent:
    match: IntentMatch
    intent: IntentDefinition
    mention_index: int


class IntentGraphPlanner:
    def plan(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
    ) -> ExecutionGraphState:
        planned = self._ordered_matches(message=message, matches=matches, intents_by_code=intents_by_code)
        graph = ExecutionGraphState(
            source_message=message,
            status=GraphStatus.WAITING_CONFIRMATION if len(planned) > 1 else GraphStatus.DRAFT,
            actions=self._default_actions(len(planned)),
        )

        for index, item in enumerate(planned):
            graph.nodes.append(
                GraphNodeState(
                    intent_code=item.intent.intent_code,
                    title=item.intent.name,
                    confidence=item.match.confidence,
                    position=index,
                )
            )

        if self._has_conditional_shape(message) and len(graph.nodes) >= 2:
            self._link(graph, 0, 1, relation_type=GraphEdgeType.CONDITIONAL, label="如果上一步成功则继续执行")
            for index in range(2, len(graph.nodes)):
                self._link(graph, index - 1, index, relation_type=GraphEdgeType.SEQUENTIAL, label="按对话顺序继续")
            graph.summary = f"识别到 {len(graph.nodes)} 个事项，包含条件依赖，确认后开始执行"
            return graph

        if self._has_sequence_shape(message) and len(graph.nodes) >= 2:
            for index in range(1, len(graph.nodes)):
                self._link(graph, index - 1, index, relation_type=GraphEdgeType.SEQUENTIAL, label="按对话顺序执行")
            graph.summary = f"识别到 {len(graph.nodes)} 个事项，已按对话顺序编排执行图"
            return graph

        graph.summary = (
            "识别到多个事项，已生成动态执行图"
            if len(graph.nodes) > 1
            else f"识别到事项：{graph.nodes[0].title}" if graph.nodes else "未识别到明确事项"
        )
        return graph

    def _ordered_matches(
        self,
        *,
        message: str,
        matches: list[IntentMatch],
        intents_by_code: dict[str, IntentDefinition],
    ) -> list[_PlannedIntent]:
        seen_intents: set[str] = set()
        ordered: list[_PlannedIntent] = []
        for match in matches:
            if match.intent_code in seen_intents:
                continue
            intent = intents_by_code.get(match.intent_code)
            if intent is None:
                continue
            seen_intents.add(match.intent_code)
            ordered.append(
                _PlannedIntent(
                    match=match,
                    intent=intent,
                    mention_index=self._mention_index(message, intent),
                )
            )
        ordered.sort(key=lambda item: (item.mention_index, item.intent.dispatch_priority * -1, item.intent.intent_code))
        return ordered

    def _mention_index(self, message: str, intent: IntentDefinition) -> int:
        lowered = message.lower()
        positions: list[int] = []
        for pattern in sorted(extract_patterns(intent), key=len, reverse=True):
            index = lowered.find(pattern.lower())
            if index >= 0:
                positions.append(index)
        return min(positions) if positions else 10_000 + max(0, 999 - intent.dispatch_priority)

    def _link(
        self,
        graph: ExecutionGraphState,
        source_index: int,
        target_index: int,
        *,
        relation_type: GraphEdgeType,
        label: str,
    ) -> None:
        source = graph.nodes[source_index]
        target = graph.nodes[target_index]
        if source.node_id not in target.depends_on:
            target.depends_on.append(source.node_id)
        target.relation_reason = label
        graph.edges.append(
            GraphEdge(
                source_node_id=source.node_id,
                target_node_id=target.node_id,
                relation_type=relation_type,
                label=label,
                condition=(
                    GraphCondition(
                        source_node_id=source.node_id,
                        expected_statuses=["completed"],
                        expression=label,
                    )
                    if relation_type == GraphEdgeType.CONDITIONAL
                    else None
                ),
            )
        )

    def _default_actions(self, node_count: int) -> list[GraphAction]:
        if node_count <= 1:
            return []
        return [
            GraphAction(code="confirm_graph", label="开始执行"),
            GraphAction(code="cancel_graph", label="取消"),
        ]

    def _has_sequence_shape(self, message: str) -> bool:
        return any(cue in message for cue in SEQUENCE_CUES)

    def _has_conditional_shape(self, message: str) -> bool:
        return any(cue in message for cue in CONDITIONAL_CUES)
