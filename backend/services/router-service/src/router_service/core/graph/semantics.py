from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphCondition, GraphEdge, GraphEdgeType, GraphNodeState


_CONTEXT_KEY_ALIASES: dict[str, set[str]] = {
    "balance": {
        "balance",
        "account_balance",
        "available_balance",
        "remaining_balance",
        "post_transfer_balance",
        "balance_after_transfer",
        "left_balance",
    },
    "due_amount": {"due_amount", "statement_amount", "repayment_amount"},
    "minimum_due": {"minimum_due", "minimum_payment"},
    "exchanged_amount": {"exchanged_amount", "converted_amount"},
}

_DEFAULT_INTENT_OUTPUT_KEYS: dict[str, set[str]] = {
    "query_account_balance": {"balance"},
    "transfer_money": {"amount", "business_status"},
    "query_credit_card_repayment": {"due_amount", "minimum_due", "due_date"},
    "pay_gas_bill": {"amount", "business_status"},
    "exchange_forex": {"exchanged_amount", "source_currency", "target_currency", "business_status"},
}


@dataclass(slots=True)
class GraphSemanticRepairResult:
    """Summary of implicit graph repairs applied after planning/building."""

    inserted_nodes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def canonical_context_key(key: str | None) -> str | None:
    """Normalize synonymous output/context keys into one canonical key."""
    if key is None:
        return None
    normalized = str(key).strip().lower()
    if not normalized:
        return None
    for canonical, aliases in _CONTEXT_KEY_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
    return normalized


def intent_output_context_keys(intent: IntentDefinition) -> set[str]:
    """Return the set of context keys an intent may produce for downstream conditions."""
    configured = {canonical_context_key(item) or str(item).strip().lower() for item in intent.graph_build_hints.provides_context_keys}
    fallback = {canonical_context_key(item) or item for item in _DEFAULT_INTENT_OUTPUT_KEYS.get(intent.intent_code, set())}
    return {item for item in configured | fallback if item}


def resolve_output_value(payload: dict[str, Any], left_key: str | None) -> Any | None:
    """Resolve a condition operand from node output payload or nested slot memory."""
    if not payload or not left_key:
        return None
    if left_key in payload:
        return payload[left_key]

    canonical = canonical_context_key(left_key)
    for key, value in payload.items():
        if canonical_context_key(key) == canonical:
            return value

    slot_memory = payload.get("slot_memory")
    if isinstance(slot_memory, dict):
        if left_key in slot_memory:
            return slot_memory[left_key]
        for key, value in slot_memory.items():
            if canonical_context_key(key) == canonical:
                return value
    return None


def repair_unexecutable_condition_edges(
    *,
    graph: ExecutionGraphState,
    intents_by_code: dict[str, IntentDefinition],
) -> GraphSemanticRepairResult:
    """Repair condition edges that reference data not produced by their source node."""
    result = GraphSemanticRepairResult()
    producer_by_source_and_key: dict[tuple[str, str], GraphNodeState] = {}

    for edge in list(graph.edges):
        condition = edge.condition
        if condition is None or condition.left_key is None:
            continue
        canonical_key = canonical_context_key(condition.left_key)
        if canonical_key is None:
            continue

        source_node = graph.node_by_id(edge.source_node_id)
        target_node = graph.node_by_id(edge.target_node_id)
        if _node_provides_context_key(source_node, intents_by_code=intents_by_code, context_key=canonical_key):
            continue

        producer = producer_by_source_and_key.get((source_node.node_id, canonical_key))
        if producer is None:
            producer = _find_existing_condition_source(
                graph=graph,
                source_node=source_node,
                target_node=target_node,
                intents_by_code=intents_by_code,
                context_key=canonical_key,
            )
        if producer is None:
            producer_intent = _select_implicit_condition_intent(
                intents_by_code=intents_by_code,
                context_key=canonical_key,
                excluded_intent_code=source_node.intent_code,
            )
            if producer_intent is None:
                continue
            producer = _insert_implicit_condition_node(
                graph=graph,
                source_node=source_node,
                target_node=target_node,
                producer_intent=producer_intent,
                context_key=canonical_key,
            )
            result.inserted_nodes.append(producer.node_id)
            result.notes.append(
                f"补充节点「{producer.title}」，用于判断节点「{target_node.title}」依赖的字段 {condition.left_key}"
            )
        producer_by_source_and_key[(source_node.node_id, canonical_key)] = producer
        _rewire_condition_edge(
            edge=edge,
            condition=condition,
            target_node=target_node,
            new_source_node=producer,
            old_source_node=source_node,
        )

    if result.inserted_nodes:
        _reindex_positions(graph)
        note = "；".join(result.notes)
        summary = graph.summary.strip()
        graph.summary = f"{summary}。系统已补充隐含条件节点：{note}" if summary else f"系统已补充隐含条件节点：{note}"
    return result


def _node_provides_context_key(
    node: GraphNodeState,
    *,
    intents_by_code: dict[str, IntentDefinition],
    context_key: str,
) -> bool:
    """Return whether the node's intent is known to produce the requested context key."""
    intent = intents_by_code.get(node.intent_code)
    if intent is None:
        return False
    return context_key in intent_output_context_keys(intent)


def _find_existing_condition_source(
    *,
    graph: ExecutionGraphState,
    source_node: GraphNodeState,
    target_node: GraphNodeState,
    intents_by_code: dict[str, IntentDefinition],
    context_key: str,
) -> GraphNodeState | None:
    """Find an already-present graph node that can produce the requested condition key."""
    candidates = [
        node
        for node in graph.nodes
        if node.node_id not in {source_node.node_id, target_node.node_id}
        and source_node.node_id in node.depends_on
        and _node_provides_context_key(node, intents_by_code=intents_by_code, context_key=context_key)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.position, item.created_at))
    return candidates[0]


def _select_implicit_condition_intent(
    *,
    intents_by_code: dict[str, IntentDefinition],
    context_key: str,
    excluded_intent_code: str,
) -> IntentDefinition | None:
    """Select the implicit producer intent when there is exactly one safe candidate."""
    candidates = [
        intent
        for intent in intents_by_code.values()
        if intent.intent_code != excluded_intent_code and context_key in intent_output_context_keys(intent)
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _insert_implicit_condition_node(
    *,
    graph: ExecutionGraphState,
    source_node: GraphNodeState,
    target_node: GraphNodeState,
    producer_intent: IntentDefinition,
    context_key: str,
) -> GraphNodeState:
    """Insert an implicit producer node required for a condition edge to become executable."""
    title = producer_intent.name
    if context_key == "balance":
        title = "查询账户余额"
    insert_position = max(target_node.position, source_node.position + 1)
    for node in graph.nodes:
        if node.position >= insert_position:
            node.position += 1
    producer = GraphNodeState(
        intent_code=producer_intent.intent_code,
        title=title,
        confidence=max(0.5, min(0.99, source_node.confidence)),
        position=insert_position,
        source_fragment="",
        relation_reason=f"为判断后续条件先执行「{title}」",
    )
    producer.depends_on.append(source_node.node_id)
    graph.nodes.append(producer)
    graph.edges.append(
        GraphEdge(
            source_node_id=source_node.node_id,
            target_node_id=producer.node_id,
            relation_type=GraphEdgeType.SEQUENTIAL,
            label=_implicit_edge_label(context_key=context_key, producer_title=title),
        )
    )
    return producer


def _rewire_condition_edge(
    *,
    edge: GraphEdge,
    condition: GraphCondition,
    target_node: GraphNodeState,
    new_source_node: GraphNodeState,
    old_source_node: GraphNodeState,
) -> None:
    """Retarget a condition edge from the old source node to the new producer node."""
    edge.source_node_id = new_source_node.node_id
    condition.source_node_id = new_source_node.node_id
    if old_source_node.node_id in target_node.depends_on:
        target_node.depends_on = [
            new_source_node.node_id if node_id == old_source_node.node_id else node_id
            for node_id in target_node.depends_on
        ]
    elif new_source_node.node_id not in target_node.depends_on:
        target_node.depends_on.append(new_source_node.node_id)


def _reindex_positions(graph: ExecutionGraphState) -> None:
    """Normalize node positions after implicit node insertion."""
    graph.nodes.sort(key=lambda node: (node.position, node.created_at))
    for index, node in enumerate(graph.nodes):
        node.position = index


def _implicit_edge_label(*, context_key: str, producer_title: str) -> str:
    """Build a user-facing edge label for an implicitly inserted producer node."""
    if context_key == "balance":
        return "为判断条件先查询余额"
    return f"为判断条件先执行 {producer_title}"
