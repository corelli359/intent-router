from __future__ import annotations

import asyncio

import httpx
import sys
from pathlib import Path

from router_service.api.app import create_router_app
from router_service.api.dependencies import (
    get_event_broker,
    get_event_broker_v2,
    get_orchestrator,
    get_orchestrator_v2,
)
from router_service.api.sse.broker import EventBroker


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.support.mock_agent_client import MockStreamingAgentClient
from router_service.core.agent_client import StreamingAgentClient
from router_service.core.domain import IntentDefinition, IntentMatch
from router_service.core.recognizer import RecognitionResult
from router_service.core.v2_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphStatus,
    ProactiveRecommendationPayload,
    ProactiveRecommendationRouteDecision,
    ProactiveRecommendationRouteMode,
)
from router_service.core.v2_orchestrator import GraphRouterOrchestrator
from router_service.core.v2_planner import BasicTurnInterpreter, SequentialIntentGraphPlanner


class _StaticCatalog:
    def __init__(self, intents: list[IntentDefinition]) -> None:
        self._intents = intents

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def get_fallback_intent(self) -> IntentDefinition | None:
        return next((intent for intent in self._intents if intent.is_fallback), None)


def _mock_intents() -> list[IntentDefinition]:
    return [
        IntentDefinition(
            intent_code="query_account_balance",
            name="查询账户余额",
            description="查询账户余额，需要卡号和手机号后4位。",
            examples=["帮我查一下账户余额", "查余额"],
            keywords=["余额", "账户", "银行卡"],
            agent_url="http://test-agent/query_account_balance",
            dispatch_priority=100,
            primary_threshold=0.68,
            candidate_threshold=0.45,
            slot_schema=[
                {
                    "slot_key": "card_number",
                    "label": "卡号",
                    "description": "银行卡号",
                    "value_type": "account_number",
                    "required": True,
                    "allow_from_history": True,
                },
                {
                    "slot_key": "phone_last_four",
                    "label": "手机号后4位",
                    "description": "手机号后4位",
                    "value_type": "phone_last4",
                    "required": True,
                    "allow_from_history": True,
                },
            ],
            graph_build_hints={"provides_context_keys": ["balance"]},
        ),
        IntentDefinition(
            intent_code="transfer_money",
            name="转账",
            description="执行转账，需要收款人姓名、收款卡号、手机号后4位和金额。",
            examples=["给张三转 200 元", "帮我转账"],
            keywords=["转账", "付款", "汇款"],
            agent_url="http://test-agent/transfer_money",
            dispatch_priority=95,
            primary_threshold=0.72,
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
                {
                    "slot_key": "recipient_card_number",
                    "label": "收款卡号",
                    "description": "收款银行卡号",
                    "value_type": "account_number",
                    "required": True,
                    "allow_from_history": False,
                },
                {
                    "slot_key": "recipient_phone_last_four",
                    "label": "收款人手机号后4位",
                    "description": "收款人手机号后4位",
                    "value_type": "phone_last4",
                    "required": True,
                    "allow_from_history": False,
                },
            ],
            graph_build_hints={"provides_context_keys": ["amount", "business_status"]},
        ),
        IntentDefinition(
            intent_code="exchange_forex",
            name="换外汇",
            description="执行换汇，需要币种和金额。",
            examples=["把100人民币换成美元", "换100美元"],
            keywords=["换汇", "购汇", "外汇"],
            agent_url="http://test-agent/exchange_forex",
            dispatch_priority=90,
            primary_threshold=0.72,
            candidate_threshold=0.5,
            slot_schema=[
                {
                    "slot_key": "source_currency",
                    "label": "卖出币种",
                    "description": "卖出币种",
                    "value_type": "string",
                    "required": True,
                },
                {
                    "slot_key": "target_currency",
                    "label": "买入币种",
                    "description": "买入币种",
                    "value_type": "string",
                    "required": True,
                },
                {
                    "slot_key": "amount",
                    "label": "金额",
                    "description": "换汇金额",
                    "value_type": "currency",
                    "required": True,
                },
            ],
            graph_build_hints={"provides_context_keys": ["exchanged_amount", "source_currency", "target_currency"]},
        ),
        IntentDefinition(
            intent_code="query_credit_card_repayment",
            name="查询信用卡还款信息",
            description="查询信用卡账单，需要卡号和手机号后4位。",
            examples=["查一下信用卡还款信息", "我这期信用卡要还多少钱"],
            keywords=["信用卡", "还款", "账单"],
            agent_url="http://test-agent/query_credit_card_repayment",
            dispatch_priority=88,
            primary_threshold=0.7,
            candidate_threshold=0.5,
            slot_schema=[
                {
                    "slot_key": "card_number",
                    "label": "信用卡卡号",
                    "description": "信用卡卡号",
                    "value_type": "account_number",
                    "required": True,
                    "allow_from_history": True,
                },
                {
                    "slot_key": "phone_last_four",
                    "label": "手机号后4位",
                    "description": "手机号后4位",
                    "value_type": "phone_last4",
                    "required": True,
                    "allow_from_history": True,
                },
            ],
            graph_build_hints={"provides_context_keys": ["due_amount", "minimum_due", "due_date"]},
        ),
        IntentDefinition(
            intent_code="pay_gas_bill",
            name="缴纳天然气费",
            description="缴纳天然气费，需要燃气户号和缴费金额。",
            examples=["给燃气户号88001234交88元", "帮我缴一下天然气费"],
            keywords=["天然气", "燃气", "缴费"],
            agent_url="http://test-agent/pay_gas_bill",
            dispatch_priority=89,
            primary_threshold=0.7,
            candidate_threshold=0.5,
            slot_schema=[
                {
                    "slot_key": "gas_account_number",
                    "label": "燃气户号",
                    "description": "燃气缴费户号",
                    "value_type": "account_number",
                    "required": True,
                },
                {
                    "slot_key": "amount",
                    "label": "缴费金额",
                    "description": "天然气缴费金额",
                    "value_type": "currency",
                    "required": True,
                },
            ],
            graph_build_hints={"provides_context_keys": ["amount", "business_status"]},
        ),
    ]


class _MessageRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        if "查余额" in message and "转账" in message:
            return RecognitionResult(
                primary=[
                    IntentMatch(intent_code="query_account_balance", confidence=0.97, reason="fixed"),
                    IntentMatch(intent_code="transfer_money", confidence=0.92, reason="fixed"),
                ],
                candidates=[],
            )
        if "查余额" in message or "查询一下余额" in message:
            return RecognitionResult(
                primary=[IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="fixed")],
                candidates=[],
            )
        if "转账" in message:
            return RecognitionResult(
                primary=[IntentMatch(intent_code="transfer_money", confidence=0.95, reason="fixed")],
                candidates=[],
            )
        return RecognitionResult(primary=[], candidates=[])


class _ConditionalPlanner:
    async def plan(self, *, message, matches, intents_by_code, recent_messages=None, long_term_memory=None):
        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到 3 个事项，包含余额条件依赖",
            status=GraphStatus.WAITING_CONFIRMATION,
        )
        balance = GraphNodeState(
            intent_code="query_account_balance",
            title="查询账户余额",
            confidence=0.97,
            position=0,
            source_fragment=message,
        )
        transfer_a = GraphNodeState(
            intent_code="transfer_money",
            title="给我媳妇儿转账 1000 元",
            confidence=0.92,
            position=1,
            source_fragment="给我媳妇儿转1000",
            slot_memory={"recipient_name": "我媳妇儿", "amount": "1000"},
        )
        transfer_b = GraphNodeState(
            intent_code="transfer_money",
            title="给我弟弟转账 1000 元",
            confidence=0.91,
            position=2,
            source_fragment="给我弟弟转1000",
            slot_memory={"recipient_name": "我弟弟", "amount": "1000"},
        )
        transfer_a.depends_on.append(balance.node_id)
        transfer_b.depends_on.append(balance.node_id)
        transfer_a.relation_reason = "当余额 > 8000 时执行"
        transfer_b.relation_reason = "当余额 > 5000 时执行"
        graph.nodes.extend([balance, transfer_a, transfer_b])
        graph.edges.extend(
            [
                GraphEdge(
                    source_node_id=balance.node_id,
                    target_node_id=transfer_a.node_id,
                    relation_type=GraphEdgeType.CONDITIONAL,
                    label="当余额 > 8000 时执行",
                    condition=GraphCondition(
                        source_node_id=balance.node_id,
                        left_key="balance",
                        operator=">",
                        right_value=8000,
                    ),
                ),
                GraphEdge(
                    source_node_id=balance.node_id,
                    target_node_id=transfer_b.node_id,
                    relation_type=GraphEdgeType.CONDITIONAL,
                    label="当余额 > 5000 时执行",
                    condition=GraphCondition(
                        source_node_id=balance.node_id,
                        left_key="balance",
                        operator=">",
                        right_value=5000,
                    ),
                ),
            ]
        )
        graph.actions = [
            GraphAction(code="confirm_graph", label="开始执行"),
            GraphAction(code="cancel_graph", label="取消"),
        ]
        return graph


class _ImplicitBalanceAfterTransferGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到转账和条件换汇，条件依赖暂挂在转账节点上",
            status=GraphStatus.WAITING_CONFIRMATION,
            actions=[
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ],
        )
        transfer = GraphNodeState(
            intent_code="transfer_money",
            title="给小明转账1000元",
            confidence=0.96,
            position=0,
            source_fragment="给小明转账1000元",
            slot_memory={"recipient_name": "小明", "amount": "1000"},
        )
        forex = GraphNodeState(
            intent_code="exchange_forex",
            title="条件满足时换100美元",
            confidence=0.94,
            position=1,
            source_fragment="把100人民币换成美元",
            slot_memory={"source_currency": "CNY", "target_currency": "USD", "amount": "100"},
        )
        forex.depends_on.append(transfer.node_id)
        forex.relation_reason = "转账后若卡里余额剩余超过2000则换汇"
        graph.nodes.extend([transfer, forex])
        graph.edges.append(
            GraphEdge(
                source_node_id=transfer.node_id,
                target_node_id=forex.node_id,
                relation_type=GraphEdgeType.CONDITIONAL,
                label="转账后若卡里余额剩余超过2000则换汇",
                condition=GraphCondition(
                    source_node_id=transfer.node_id,
                    left_key="balance",
                    operator=">",
                    right_value=2000,
                ),
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": RecognitionResult(
                    primary=[
                        IntentMatch(intent_code="transfer_money", confidence=0.96, reason="fixed"),
                        IntentMatch(intent_code="exchange_forex", confidence=0.94, reason="fixed"),
                    ],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


class _ExplodingRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        raise AssertionError("guided selection should bypass recognizer")


class _RecommendationAwareRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del intents, long_term_memory, on_delta
        assert message == "第一个和第三个都要"
        recommendation_context = next(
            (entry for entry in recent_messages if entry.startswith("[FRONTEND_RECOMMENDATION_CONTEXT]")),
            None,
        )
        assert recommendation_context is not None
        assert "查询账户余额" in recommendation_context
        assert "换外汇" in recommendation_context
        return RecognitionResult(
            primary=[
                IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="picked from recommendation"),
                IntentMatch(intent_code="exchange_forex", confidence=0.93, reason="picked from recommendation"),
            ],
            candidates=[],
        )


class _ProactiveFreeDialogRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del intents, long_term_memory, on_delta
        assert message == "我想换100美元"
        assert not any(entry.startswith("[PROACTIVE_RECOMMENDATION_SELECTION]") for entry in recent_messages)
        return RecognitionResult(
            primary=[IntentMatch(intent_code="exchange_forex", confidence=0.96, reason="free dialog switch")],
            candidates=[],
        )


class _StaticRecommendationRouter:
    def __init__(self, decision: ProactiveRecommendationRouteDecision) -> None:
        self._decision = decision

    async def decide(self, *, message, proactive_recommendation):
        del message, proactive_recommendation
        return self._decision.model_copy(deep=True)


class _ProactiveInteractiveGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, long_term_memory, on_delta
        assert message == "第一个，但是金额改成500"
        assert recognition is not None
        assert [match.intent_code for match in recognition.primary] == ["transfer_money"]
        proactive_selection = next(
            (entry for entry in recent_messages if entry.startswith("[PROACTIVE_RECOMMENDATION_SELECTION]")),
            None,
        )
        assert proactive_selection is not None
        assert "给妈妈转账2000元" in proactive_selection
        graph = ExecutionGraphState(
            source_message=message,
            summary="已根据推荐项和用户修改重建执行图",
            status=GraphStatus.DRAFT,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="transfer_money",
                title="给妈妈转账500元",
                confidence=0.98,
                position=0,
                source_fragment=message,
                slot_memory={"amount": "500"},
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": recognition,
                "graph": graph,
            },
        )()


class _ProactiveConditionalRepairGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, recent_messages, long_term_memory, on_delta
        assert message == "我选择缴天然气费和转账，如果余额超过2000，那么就给我妈妈转3000"
        assert recognition is not None
        graph = ExecutionGraphState(
            source_message=message,
            summary="已根据推荐项和条件要求生成执行图",
            status=GraphStatus.WAITING_CONFIRMATION,
            actions=[
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ],
        )
        gas = GraphNodeState(
            intent_code="pay_gas_bill",
            title="缴纳天然气费",
            confidence=0.97,
            position=0,
            source_fragment="缴天然气费",
            slot_memory={},
        )
        transfer = GraphNodeState(
            intent_code="transfer_money",
            title="给妈妈转3000",
            confidence=0.95,
            position=1,
            source_fragment="如果余额超过2000，那么就给我妈妈转3000",
            slot_memory={"recipient_name": "妈妈", "amount": "3000"},
        )
        transfer.depends_on.append(gas.node_id)
        transfer.relation_reason = "余额超过2000时执行转账"
        graph.nodes.extend([gas, transfer])
        graph.edges.append(
            GraphEdge(
                source_node_id=gas.node_id,
                target_node_id=transfer.node_id,
                relation_type=GraphEdgeType.CONDITIONAL,
                label="余额超过2000时执行转账",
                condition=GraphCondition(
                    source_node_id=gas.node_id,
                    left_key="balance",
                    operator=">",
                    right_value=2000,
                ),
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": recognition,
                "graph": graph,
            },
        )()


def _test_v2_app(
    *,
    recognizer=None,
    graph_builder=None,
    planner=None,
    turn_interpreter=None,
    recommendation_router=None,
) -> tuple[object, GraphRouterOrchestrator]:
    broker = EventBroker()
    orchestrator = GraphRouterOrchestrator(
        publish_event=broker.publish,
        intent_catalog=_StaticCatalog(_mock_intents()),
        recognizer=recognizer or _MessageRecognizer(),
        graph_builder=graph_builder,
        planner=planner or SequentialIntentGraphPlanner(),
        turn_interpreter=turn_interpreter or BasicTurnInterpreter(),
        recommendation_router=recommendation_router,
        agent_client=MockStreamingAgentClient(),
    )
    app = create_router_app()
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_orchestrator_v2] = lambda: orchestrator
    app.dependency_overrides[get_event_broker] = lambda: broker
    app.dependency_overrides[get_event_broker_v2] = lambda: broker
    return app, orchestrator


class _SingleNodeConfirmGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到 1 个高风险事项，需要确认后执行",
            status=GraphStatus.WAITING_CONFIRMATION,
            actions=[
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ],
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="transfer_money",
                title="给我媳妇儿转1000元",
                confidence=0.97,
                position=0,
                source_fragment=message,
                slot_memory={"recipient_name": "我媳妇儿", "amount": "1000"},
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": RecognitionResult(
                    primary=[IntentMatch(intent_code="transfer_money", confidence=0.97, reason="fixed")],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


class _RecentMessagesRecordingGraphBuilder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, long_term_memory, on_delta
        self.calls.append(list(recent_messages))
        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到 1 个事项，直接执行",
            status=GraphStatus.DRAFT,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳天然气费",
                confidence=0.95,
                position=0,
                source_fragment="给燃气户号88001234交88元",
                slot_memory={
                    "gas_account_number": "88001234",
                    "amount": "88",
                },
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": recognition
                or RecognitionResult(
                    primary=[IntentMatch(intent_code="pay_gas_bill", confidence=0.95, reason="fixed")],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


class _HistoryPrefillGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, recent_messages, long_term_memory, recognition, on_delta
        if "卡号" in message or "尾号" in message:
            graph = ExecutionGraphState(
                source_message=message,
                summary="识别到余额查询",
                status=GraphStatus.DRAFT,
            )
            graph.nodes.append(
                GraphNodeState(
                    intent_code="query_account_balance",
                    title="查询账户余额",
                    confidence=0.96,
                    position=0,
                    source_fragment=message,
                    slot_memory={
                        "card_number": "6222021234567890",
                        "phone_last_four": "1234",
                    },
                )
            )
            return type(
                "GraphBuildResult",
                (),
                {
                    "recognition": RecognitionResult(
                        primary=[IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="grounded")],
                        candidates=[],
                    ),
                    "graph": graph,
                },
            )()
        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到余额查询",
            status=GraphStatus.DRAFT,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="query_account_balance",
                title="查询账户余额",
                confidence=0.96,
                position=0,
                source_fragment=message,
                slot_memory={
                    "card_number": "6222021234567890",
                    "phone_last_four": "1234",
                },
                history_slot_keys=["card_number", "phone_last_four"],
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": RecognitionResult(
                    primary=[IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="history")],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


class _HistoryConditionalGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, recent_messages, long_term_memory, recognition, on_delta
        if "卡号" in message or "尾号" in message:
            graph = ExecutionGraphState(
                source_message=message,
                summary="识别到余额查询",
                status=GraphStatus.DRAFT,
            )
            graph.nodes.append(
                GraphNodeState(
                    intent_code="query_account_balance",
                    title="查询账户余额",
                    confidence=0.96,
                    position=0,
                    source_fragment=message,
                    slot_memory={
                        "card_number": "6222021234567890",
                        "phone_last_four": "1234",
                    },
                )
            )
            return type(
                "GraphBuildResult",
                (),
                {
                    "recognition": RecognitionResult(
                        primary=[IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="grounded")],
                        candidates=[],
                    ),
                    "graph": graph,
                },
            )()

        graph = ExecutionGraphState(
            source_message=message,
            summary="先查余额，如果余额足够就给小明转账 1000 元",
            status=GraphStatus.DRAFT,
        )
        balance = GraphNodeState(
            intent_code="query_account_balance",
            title="查询账户余额",
            confidence=0.96,
            position=0,
            source_fragment="帮我查一下余额",
            slot_memory={
                "card_number": "6222021234567890",
                "phone_last_four": "1234",
            },
        )
        transfer = GraphNodeState(
            intent_code="transfer_money",
            title="给小明转账 1000 元",
            confidence=0.92,
            position=1,
            source_fragment="如果大于199999，就给小明转账1000",
            slot_memory={"recipient_name": "小明", "amount": "1000"},
        )
        transfer.depends_on.append(balance.node_id)
        transfer.relation_reason = "当余额 > 199999 时执行"
        graph.nodes.extend([balance, transfer])
        graph.edges.append(
            GraphEdge(
                source_node_id=balance.node_id,
                target_node_id=transfer.node_id,
                relation_type=GraphEdgeType.CONDITIONAL,
                label="当余额 > 199999 时执行",
                condition=GraphCondition(
                    source_node_id=balance.node_id,
                    left_key="balance",
                    operator=">",
                    right_value=199999,
                ),
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": RecognitionResult(
                    primary=[
                        IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="history"),
                        IntentMatch(intent_code="transfer_money", confidence=0.92, reason="conditional"),
                    ],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


class _MissingSlotGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, recent_messages, long_term_memory, recognition, on_delta
        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到天然气缴费事项，等待补全槽位",
            status=GraphStatus.DRAFT,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="pay_gas_bill",
                title="缴纳天然气费",
                confidence=0.95,
                position=0,
                source_fragment=message,
                slot_memory={},
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": RecognitionResult(
                    primary=[IntentMatch(intent_code="pay_gas_bill", confidence=0.95, reason="fixed")],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


class _RateLimitedGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del message, intents, recent_messages, long_term_memory, recognition, on_delta

        class _RateLimitError(Exception):
            def __init__(self) -> None:
                super().__init__("rate limited")
                self.status_code = 429

        raise _RateLimitError()


class _FirstMatchThenRateLimitedRecognizer:
    def __init__(self) -> None:
        self.calls = 0

    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del message, intents, recent_messages, long_term_memory, on_delta
        self.calls += 1
        if self.calls == 1:
            return RecognitionResult(
                primary=[IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="fixed")],
                candidates=[],
            )

        class _RateLimitError(Exception):
            def __init__(self) -> None:
                super().__init__("rate limited")
                self.status_code = 429

        raise _RateLimitError()


class _HistoryConditionalGraphBuilder:
    async def build(self, *, message, intents, recent_messages, long_term_memory, recognition=None, on_delta=None):
        del intents, recent_messages, long_term_memory, recognition, on_delta
        if "如果大于199999" in message:
            graph = ExecutionGraphState(
                source_message=message,
                summary="先查询账户余额，如果余额大于 199999 元则给小明转账 1000 元",
                status=GraphStatus.DRAFT,
            )
            balance = GraphNodeState(
                intent_code="query_account_balance",
                title="查询账户余额",
                confidence=0.97,
                position=0,
                source_fragment="帮我查一下余额",
                slot_memory={},
            )
            transfer = GraphNodeState(
                intent_code="transfer_money",
                title="给小明转账 1000 元",
                confidence=0.91,
                position=1,
                source_fragment="如果大于199999，就给小明转账1000",
                slot_memory={"recipient_name": "小明", "amount": "1000"},
            )
            transfer.depends_on.append(balance.node_id)
            transfer.relation_reason = "余额大于 199999 时转账"
            graph.nodes.extend([balance, transfer])
            graph.edges.append(
                GraphEdge(
                    source_node_id=balance.node_id,
                    target_node_id=transfer.node_id,
                    relation_type=GraphEdgeType.CONDITIONAL,
                    label="余额大于 199999 时转账",
                    condition=GraphCondition(
                        source_node_id=balance.node_id,
                        left_key="balance",
                        operator=">",
                        right_value=199999,
                    ),
                )
            )
            return type(
                "GraphBuildResult",
                (),
                {
                    "recognition": RecognitionResult(
                        primary=[
                            IntentMatch(intent_code="query_account_balance", confidence=0.97, reason="history"),
                            IntentMatch(intent_code="transfer_money", confidence=0.91, reason="conditional"),
                        ],
                        candidates=[],
                    ),
                    "graph": graph,
                },
            )()

        graph = ExecutionGraphState(
            source_message=message,
            summary="识别到余额查询",
            status=GraphStatus.DRAFT,
        )
        graph.nodes.append(
            GraphNodeState(
                intent_code="query_account_balance",
                title="查询账户余额",
                confidence=0.96,
                position=0,
                source_fragment=message,
                slot_memory={
                    "card_number": "6222021234567890",
                    "phone_last_four": "1234",
                },
            )
        )
        return type(
            "GraphBuildResult",
            (),
            {
                "recognition": RecognitionResult(
                    primary=[IntentMatch(intent_code="query_account_balance", confidence=0.96, reason="seed")],
                    candidates=[],
                ),
                "graph": graph,
            },
        )()


def test_v2_main_prefix_alias_matches_primary_graph_runtime() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/sessions/{session_id}/messages",
                json={"content": "先查余额，再给张三转账 200 元，卡号 6222020100049999999，尾号 1234"},
            )
            assert response.status_code == 200
            pending_graph = response.json()["snapshot"]["pending_graph"]
            assert pending_graph is not None
            assert pending_graph["status"] == "waiting_confirmation"

            alias_response = await client.get(f"/api/router/v2/sessions/{session_id}")
            assert alias_response.status_code == 200
            assert alias_response.json()["pending_graph"]["graph_id"] == pending_graph["graph_id"]

    asyncio.run(run())


def test_v2_multi_intent_graph_requires_confirmation_and_runs_sequentially() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "先查余额，再给张三转账 200 元，卡号 6222020100049999999，尾号 1234"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["pending_graph"]["status"] == "waiting_confirmation"
            assert len(snapshot["pending_graph"]["nodes"]) == 2
            assert snapshot["pending_graph"]["edges"][0]["relation_type"] == "sequential"

            graph = snapshot["pending_graph"]
            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200
            confirmed_snapshot = confirm_turn.json()["snapshot"]
            assert confirmed_snapshot["current_graph"]["status"] in {
                "waiting_user_input",
                "partially_completed",
                "completed",
            }
            assert confirmed_snapshot["pending_graph"] is None

    asyncio.run(run())


def test_v2_single_node_waiting_confirmation_from_unified_builder_stays_pending() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(graph_builder=_SingleNodeConfirmGraphBuilder())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "给我媳妇儿转1000"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["pending_graph"] is not None
            assert snapshot["pending_graph"]["status"] == "waiting_confirmation"
            assert len(snapshot["pending_graph"]["nodes"]) == 1
            assert snapshot["current_graph"] is None

    asyncio.run(run())


def test_v2_new_graph_planning_ignores_assistant_execution_messages_in_recent_context() -> None:
    builder = _RecentMessagesRecordingGraphBuilder()

    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_ExplodingRecognizer(),
            graph_builder=builder,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我处理一下这个事项"},
            )
            assert first_turn.status_code == 200
            first_snapshot = first_turn.json()["snapshot"]
            assert any("已为燃气户号 88001234 缴费 88 元" in message["content"] for message in first_snapshot["messages"])

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我处理一下这个事项"},
            )
            assert second_turn.status_code == 200

            assert len(builder.calls) == 2
            assert builder.calls[0] == ["user: 帮我处理一下这个事项"]
            assert builder.calls[1] == [
                "user: 帮我处理一下这个事项",
                "user: 帮我处理一下这个事项",
            ]
            assert all(not entry.startswith("assistant:") for entry in builder.calls[1])

    asyncio.run(run())


def test_v2_history_prefill_requires_confirmation_before_execution() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(graph_builder=_HistoryPrefillGraphBuilder())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            seed_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "卡号 6222021234567890，尾号1234"},
            )
            assert seed_turn.status_code == 200
            first_snapshot = seed_turn.json()["snapshot"]
            assert first_snapshot["current_graph"]["status"] == "completed"

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额"},
            )
            assert second_turn.status_code == 200
            snapshot = second_turn.json()["snapshot"]
            pending_graph = snapshot["pending_graph"]
            assert pending_graph is not None
            assert pending_graph["status"] == "waiting_confirmation"
            assert pending_graph["nodes"][0]["history_slot_keys"] == ["card_number", "phone_last_four"]
            assert "检测到历史信息复用" in pending_graph["summary"]
            assert snapshot["current_graph"] is None

    asyncio.run(run())


def test_v2_history_prefill_in_conditional_graph_requires_confirmation() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(graph_builder=_HistoryConditionalGraphBuilder())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]

            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额"},
            )
            assert first_turn.status_code == 200
            assert first_turn.json()["snapshot"]["current_graph"]["status"] == "waiting_user_input"

            seed_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "卡号 6222021234567890，尾号1234"},
            )
            assert seed_turn.status_code == 200
            assert seed_turn.json()["snapshot"]["current_graph"]["status"] == "completed"

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额，如果大于199999，就给小明转账1000"},
            )
            assert second_turn.status_code == 200
            snapshot = second_turn.json()["snapshot"]
            pending_graph = snapshot["pending_graph"]
            assert pending_graph is not None
            assert pending_graph["status"] == "waiting_confirmation"
            assert [node["intent_code"] for node in pending_graph["nodes"]] == [
                "query_account_balance",
                "transfer_money",
            ]
            assert pending_graph["nodes"][0]["history_slot_keys"] == ["card_number", "phone_last_four"]
            assert pending_graph["nodes"][1]["history_slot_keys"] == []
            assert "检测到历史信息复用" in pending_graph["summary"]
            assert snapshot["current_graph"] is None

    asyncio.run(run())


def test_v2_history_prefill_on_conditional_graph_requires_confirmation_and_finishes_completed() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(graph_builder=_HistoryConditionalGraphBuilder())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            seed_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额，卡号 6222021234567890，尾号1234"},
            )
            assert seed_turn.status_code == 200
            assert seed_turn.json()["snapshot"]["current_graph"]["status"] == "completed"

            conditional_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额，如果大于199999，就给小明转账1000"},
            )
            assert conditional_turn.status_code == 200
            pending_graph = conditional_turn.json()["snapshot"]["pending_graph"]
            assert pending_graph is not None
            assert pending_graph["status"] == "waiting_confirmation"
            assert pending_graph["nodes"][0]["intent_code"] == "query_account_balance"
            assert pending_graph["nodes"][0]["history_slot_keys"] == ["card_number", "phone_last_four"]
            assert "检测到历史信息复用" in pending_graph["summary"]

            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": pending_graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": pending_graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200
            snapshot = confirm_turn.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert [node["status"] for node in current_graph["nodes"]] == ["completed", "skipped"]
            assert current_graph["nodes"][1]["skip_reason_code"] == "condition_not_met"
            assert "因条件未满足未执行" in snapshot["messages"][-1]["content"]

    asyncio.run(run())


def test_v2_rate_limited_graph_builder_returns_busy_message() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(graph_builder=_RateLimitedGraphBuilder())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额，如果大于199999，就给小明转账1000"},
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            assert snapshot["current_graph"] is None
            assert snapshot["pending_graph"] is None
            assert snapshot["messages"][-1]["content"] == "当前意图识别服务繁忙，请稍后重试。"

    asyncio.run(run())


def test_v2_waiting_node_can_continue_when_recognizer_is_temporarily_unavailable() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(recognizer=_FirstMatchThenRateLimitedRecognizer())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额"},
            )
            assert first_turn.status_code == 200
            first_snapshot = first_turn.json()["snapshot"]
            assert first_snapshot["current_graph"]["status"] == "waiting_user_input"

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "卡号 6222021234567890，尾号1234"},
            )
            assert second_turn.status_code == 200
            second_snapshot = second_turn.json()["snapshot"]
            assert second_snapshot["current_graph"]["status"] == "completed"
            assert second_snapshot["messages"][-1]["content"] == "查询成功，账户余额为 8000 元"

    asyncio.run(run())


def test_v2_waiting_node_switches_to_new_intent() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我转账"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["current_graph"]["status"] == "waiting_user_input"
            assert snapshot["current_graph"]["nodes"][0]["intent_code"] == "transfer_money"

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "算了，帮我查余额"},
            )
            assert second_turn.status_code == 200
            switched_snapshot = second_turn.json()["snapshot"]
            assert switched_snapshot["current_graph"]["nodes"][0]["intent_code"] == "query_account_balance"
            assert switched_snapshot["current_graph"]["status"] == "waiting_user_input"

    asyncio.run(run())


def test_v2_cancel_node_action_cancels_current_graph_node() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我转账"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            node_id = snapshot["current_graph"]["nodes"][0]["node_id"]

            cancel_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": node_id,
                    "source": "router",
                    "action_code": "cancel_node",
                    "payload": {"reason": "用户主动取消"},
                },
            )
            assert cancel_turn.status_code == 200
            cancelled_snapshot = cancel_turn.json()["snapshot"]
            assert cancelled_snapshot["current_graph"]["nodes"][0]["status"] == "cancelled"
            assert cancelled_snapshot["current_graph"]["status"] == "cancelled"

    asyncio.run(run())


def test_v2_runtime_fails_closed_for_mock_scheme_agent_url() -> None:
    class UnsupportedSchemeCatalog:
        def list_active(self) -> list[IntentDefinition]:
            return [
                IntentDefinition(
                    intent_code="query_account_balance",
                    name="查询账户余额",
                    description="查询账户余额",
                    examples=["帮我查一下余额"],
                    agent_url="mock://query_account_balance",
                    dispatch_priority=100,
                )
            ]

        def get_fallback_intent(self) -> IntentDefinition | None:
            return None

    async def run() -> None:
        broker = EventBroker()
        orchestrator = GraphRouterOrchestrator(
            publish_event=broker.publish,
            intent_catalog=UnsupportedSchemeCatalog(),
            recognizer=_MessageRecognizer(),
            planner=SequentialIntentGraphPlanner(),
            turn_interpreter=BasicTurnInterpreter(),
            agent_client=StreamingAgentClient(),
        )
        app = create_router_app()
        app.dependency_overrides[get_orchestrator_v2] = lambda: orchestrator
        app.dependency_overrides[get_event_broker_v2] = lambda: broker

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查余额"},
            )

        snapshot = response.json()["snapshot"]
        assert response.status_code == 200
        assert snapshot["current_graph"]["nodes"][0]["status"] == "failed"
        assert any("Unsupported agent_url scheme" in message["content"] for message in snapshot["messages"])

    asyncio.run(run())


def test_v2_expands_multi_transfer_conditions_and_skips_unsatisfied_branch() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_MessageRecognizer(),
            planner=_ConditionalPlanner(),
            turn_interpreter=BasicTurnInterpreter(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "我想查询一下余额，如果大于8000，就给我媳妇儿转1000，如果大于5000，就再给我弟弟转1000"
                },
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            pending_graph = snapshot["pending_graph"]
            assert pending_graph["status"] == "waiting_confirmation"
            assert [node["intent_code"] for node in pending_graph["nodes"]] == [
                "query_account_balance",
                "transfer_money",
                "transfer_money",
            ]
            assert [
                (
                    edge["condition"]["left_key"],
                    edge["condition"]["operator"],
                    edge["condition"]["right_value"],
                )
                for edge in pending_graph["edges"]
            ] == [
                ("balance", ">", 8000),
                ("balance", ">", 5000),
            ]
            assert pending_graph["nodes"][1]["slot_memory"]["recipient_name"] == "我媳妇儿"
            assert pending_graph["nodes"][2]["slot_memory"]["recipient_name"] == "我弟弟"

            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": pending_graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": pending_graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200

            resume_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "6222020100049999999，尾号1234"},
            )
            assert resume_turn.status_code == 200
            resumed_snapshot = resume_turn.json()["snapshot"]
            current_graph = resumed_snapshot["current_graph"]
            assert [node["status"] for node in current_graph["nodes"]] == [
                "completed",
                "skipped",
                "waiting_user_input",
            ]
            assert current_graph["nodes"][1]["blocking_reason"] == "当余额 > 8000 时执行"
            assert current_graph["nodes"][2]["slot_memory"]["recipient_name"] == "我弟弟"
            assert current_graph["nodes"][2]["slot_memory"]["amount"] == "1000"

    asyncio.run(run())


def test_v2_single_conditional_skip_marks_graph_completed_with_skip_reason() -> None:
    class _SingleConditionalPlanner:
        async def plan(self, *, message, matches, intents_by_code, recent_messages=None, long_term_memory=None):
            graph = ExecutionGraphState(
                source_message=message,
                summary="先查余额，若余额大于8000则给媳妇儿转500元",
                status=GraphStatus.WAITING_CONFIRMATION,
            )
            balance = GraphNodeState(
                intent_code="query_account_balance",
                title="查询账户余额",
                confidence=0.98,
                position=0,
                source_fragment="帮我查一下余额",
            )
            transfer = GraphNodeState(
                intent_code="transfer_money",
                title="给媳妇儿转账500元",
                confidence=0.91,
                position=1,
                source_fragment="如果超过8000，就给我媳妇儿转账500",
                slot_memory={"recipient_name": "我媳妇儿", "amount": "500"},
            )
            transfer.depends_on.append(balance.node_id)
            transfer.relation_reason = "余额大于8000时转账"
            graph.nodes.extend([balance, transfer])
            graph.edges.append(
                GraphEdge(
                    source_node_id=balance.node_id,
                    target_node_id=transfer.node_id,
                    relation_type=GraphEdgeType.CONDITIONAL,
                    label="余额大于8000时转账",
                    condition=GraphCondition(
                        source_node_id=balance.node_id,
                        left_key="balance",
                        operator=">",
                        right_value=8000,
                    ),
                )
            )
            graph.actions = [
                GraphAction(code="confirm_graph", label="开始执行"),
                GraphAction(code="cancel_graph", label="取消"),
            ]
            return graph

    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_MessageRecognizer(),
            planner=_SingleConditionalPlanner(),
            turn_interpreter=BasicTurnInterpreter(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我查一下余额，如果超过8000，就给我媳妇儿转账500"},
            )
            pending_graph = first_turn.json()["snapshot"]["pending_graph"]
            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": pending_graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": pending_graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200

            resume_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "6222020100049999999，尾号1234"},
            )
            assert resume_turn.status_code == 200
            snapshot = resume_turn.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert [node["status"] for node in current_graph["nodes"]] == ["completed", "skipped"]
            assert current_graph["nodes"][1]["blocking_reason"] == "余额大于8000时转账"
            assert current_graph["nodes"][1]["skip_reason_code"] == "condition_not_met"
            assert "因条件未满足未执行" in snapshot["messages"][-1]["content"]

    asyncio.run(run())


def test_v2_implicit_balance_condition_inserts_hidden_node_instead_of_skipping() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            graph_builder=_ImplicitBalanceAfterTransferGraphBuilder(),
            turn_interpreter=BasicTurnInterpreter(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "我想给小明转账1000元，如果卡里余额还剩超过2000，我就换100美元"},
            )
            assert first_turn.status_code == 200
            pending_graph = first_turn.json()["snapshot"]["pending_graph"]
            assert [node["intent_code"] for node in pending_graph["nodes"]] == [
                "transfer_money",
                "query_account_balance",
                "exchange_forex",
            ]
            conditional_edge = next(
                edge for edge in pending_graph["edges"] if edge["relation_type"] == "conditional"
            )
            assert conditional_edge["condition"]["left_key"] == "balance"

            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": pending_graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": pending_graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200

            resume_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "收款卡号 6222020100049999999，手机号后四位1234；我的卡号 6222021234567890，尾号1234"},
            )
            assert resume_turn.status_code == 200
            snapshot = resume_turn.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert [node["intent_code"] for node in current_graph["nodes"]] == [
                "transfer_money",
                "query_account_balance",
                "exchange_forex",
            ]
            assert [node["status"] for node in current_graph["nodes"]] == [
                "completed",
                "completed",
                "completed",
            ]
            assert all(node["skip_reason_code"] is None for node in current_graph["nodes"])
            assert "因条件未满足未执行" not in snapshot["messages"][-1]["content"]

    asyncio.run(run())


def test_v2_repeating_same_conditional_message_keeps_graph_shape_and_condition() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            graph_builder=_ImplicitBalanceAfterTransferGraphBuilder(),
            turn_interpreter=BasicTurnInterpreter(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            message = "我想给小明转账1000元，如果卡里余额还剩超过2000，我就换100美元"

            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": message},
            )
            first_pending_graph = first_turn.json()["snapshot"]["pending_graph"]
            assert [node["intent_code"] for node in first_pending_graph["nodes"]] == [
                "transfer_money",
                "query_account_balance",
                "exchange_forex",
            ]

            await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": first_pending_graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": first_pending_graph["confirm_token"],
                },
            )
            resume_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "收款卡号 6222020100049999999，手机号后四位1234；我的卡号 6222021234567890，尾号1234"},
            )
            assert resume_turn.status_code == 200
            assert resume_turn.json()["snapshot"]["current_graph"]["status"] == "completed"

            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": message},
            )
            assert second_turn.status_code == 200
            second_pending_graph = second_turn.json()["snapshot"]["pending_graph"]
            assert second_pending_graph is not None
            assert second_pending_graph["status"] == "waiting_confirmation"
            assert [node["intent_code"] for node in second_pending_graph["nodes"]] == [
                "transfer_money",
                "query_account_balance",
                "exchange_forex",
            ]

            conditional_edge = next(
                edge
                for edge in second_pending_graph["edges"]
                if edge["relation_type"] == "conditional"
            )
            assert conditional_edge["condition"] == {
                "source_node_id": conditional_edge["source_node_id"],
                "expected_statuses": ["completed"],
                "left_key": "balance",
                "operator": ">",
                "right_value": 2000,
            }
            query_balance_node = next(
                node for node in second_pending_graph["nodes"] if node["intent_code"] == "query_account_balance"
            )
            assert conditional_edge["source_node_id"] == query_balance_node["node_id"]

    asyncio.run(run())


def test_v2_guided_selection_bypasses_recognizer_and_executes_selected_items() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(recognizer=_ExplodingRecognizer())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "guidedSelection": {
                        "selectedIntents": [
                            {
                                "intentCode": "transfer_money",
                                "title": "给小明转账1000元",
                                "slotMemory": {
                                    "recipient_name": "小明",
                                    "recipient_card_number": "6222020100049999999",
                                    "recipient_phone_last_four": "1234",
                                    "amount": "1000",
                                },
                            },
                            {
                                "intentCode": "exchange_forex",
                                "title": "换100美元",
                                "slotMemory": {
                                    "source_currency": "CNY",
                                    "target_currency": "USD",
                                    "amount": "100",
                                },
                            },
                        ]
                    }
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert [node["intent_code"] for node in current_graph["nodes"]] == [
                "transfer_money",
                "exchange_forex",
            ]
            assert [node["status"] for node in current_graph["nodes"]] == ["completed", "completed"]
            assert snapshot["messages"][0]["content"] == "已选择推荐事项：给小明转账1000元、换100美元"

    asyncio.run(run())


def test_v2_recommendation_context_still_routes_via_llm_recognition() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(recognizer=_RecommendationAwareRecognizer())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "第一个和第三个都要",
                    "recommendationContext": {
                        "recommendationId": "rec_demo",
                        "intents": [
                            {
                                "intentCode": "query_account_balance",
                                "title": "查询账户余额",
                                "description": "查账户余额",
                                "examples": ["帮我查一下账户余额"],
                            },
                            {
                                "intentCode": "transfer_money",
                                "title": "转账",
                                "description": "执行转账",
                                "examples": ["给小明转1000"],
                            },
                            {
                                "intentCode": "exchange_forex",
                                "title": "换外汇",
                                "description": "执行换汇",
                                "examples": ["换100美元"],
                            },
                        ],
                    },
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            pending_graph = snapshot["pending_graph"]
            assert pending_graph["status"] == "waiting_confirmation"
            assert [node["intent_code"] for node in pending_graph["nodes"]] == [
                "query_account_balance",
                "exchange_forex",
            ]
            assert snapshot["messages"][-1]["content"] == "第一个和第三个都要"

    asyncio.run(run())


def test_v2_proactive_recommendation_direct_execute_bypasses_free_dialog_recognizer() -> None:
    proactive_recommendation = {
        "introText": "工资到账后，这里有两项可直接执行的事项。",
        "items": [
            {
                "recommendationItemId": "rec_transfer_mom",
                "intentCode": "transfer_money",
                "title": "给妈妈转账2000元",
                "description": "沿用上次转账信息",
                "slotMemory": {
                    "recipient_name": "妈妈",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last_four": "1234",
                    "amount": "2000",
                },
                "executionPayload": {"mock": "transfer"},
                "allowDirectExecute": True,
            },
            {
                "recommendationItemId": "rec_exchange_usd",
                "intentCode": "exchange_forex",
                "title": "换100美元",
                "description": "按默认币种换汇",
                "slotMemory": {
                    "source_currency": "CNY",
                    "target_currency": "USD",
                    "amount": "100",
                },
                "executionPayload": {"mock": "forex"},
                "allowDirectExecute": True,
            },
        ],
    }

    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_ExplodingRecognizer(),
            recommendation_router=_StaticRecommendationRouter(
                ProactiveRecommendationRouteDecision(
                    route_mode=ProactiveRecommendationRouteMode.DIRECT_EXECUTE,
                    selectedRecommendationIds=["rec_transfer_mom", "rec_exchange_usd"],
                    selectedIntents=["transfer_money", "exchange_forex"],
                    hasUserModification=False,
                    reason="用户直接接受推荐原始数据",
                )
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "第一个和第二个都要",
                    "proactiveRecommendation": proactive_recommendation,
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert [node["intent_code"] for node in current_graph["nodes"]] == [
                "transfer_money",
                "exchange_forex",
            ]
            assert [node["status"] for node in current_graph["nodes"]] == ["completed", "completed"]
            assert snapshot["messages"][0]["content"] == "第一个和第二个都要"

    asyncio.run(run())


def test_v2_proactive_recommendation_interactive_graph_keeps_user_modification_for_follow_up() -> None:
    proactive_recommendation = {
        "introText": "工资到账后，这里有一项常用转账建议。",
        "items": [
            {
                "recommendationItemId": "rec_transfer_mom",
                "intentCode": "transfer_money",
                "title": "给妈妈转账2000元",
                "description": "沿用上次转账信息",
                "slotMemory": {
                    "recipient_name": "妈妈",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last_four": "1234",
                    "amount": "2000",
                },
                "executionPayload": {"mock": "transfer"},
                "allowDirectExecute": True,
            }
        ],
    }

    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_ExplodingRecognizer(),
            graph_builder=_ProactiveInteractiveGraphBuilder(),
            recommendation_router=_StaticRecommendationRouter(
                ProactiveRecommendationRouteDecision(
                    route_mode=ProactiveRecommendationRouteMode.INTERACTIVE_GRAPH,
                    selectedRecommendationIds=["rec_transfer_mom"],
                    selectedIntents=["transfer_money"],
                    hasUserModification=True,
                    modificationReasons=["用户修改了金额"],
                    reason="用户修改推荐项金额，需要重建执行图",
                )
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "第一个，但是金额改成500",
                    "proactiveRecommendation": proactive_recommendation,
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert current_graph["nodes"][0]["intent_code"] == "transfer_money"
            assert current_graph["nodes"][0]["slot_memory"]["recipient_name"] == "妈妈"
            assert current_graph["nodes"][0]["slot_memory"]["recipient_card_number"] == "6222020100049999999"
            assert current_graph["nodes"][0]["slot_memory"]["amount"] == "500"
            assert snapshot["messages"][-1]["content"] == "已向妈妈转账 500 元，转账成功"

    asyncio.run(run())


def test_v2_proactive_recommendation_switch_to_free_dialog_reuses_existing_recognition_path() -> None:
    proactive_recommendation = {
        "introText": "工资到账后，这里有一组推荐事项。",
        "items": [
            {
                "recommendationItemId": "rec_transfer_mom",
                "intentCode": "transfer_money",
                "title": "给妈妈转账2000元",
                "slotMemory": {
                    "recipient_name": "妈妈",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last_four": "1234",
                    "amount": "2000",
                },
                "executionPayload": {},
                "allowDirectExecute": True,
            }
        ],
    }

    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_ProactiveFreeDialogRecognizer(),
            recommendation_router=_StaticRecommendationRouter(
                ProactiveRecommendationRouteDecision(
                    route_mode=ProactiveRecommendationRouteMode.SWITCH_TO_FREE_DIALOG,
                    selectedRecommendationIds=[],
                    selectedIntents=[],
                    hasUserModification=False,
                    reason="用户表达了独立新诉求",
                )
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "我想换100美元",
                    "proactiveRecommendation": proactive_recommendation,
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            assert snapshot["pending_graph"] is None
            assert snapshot["current_graph"]["nodes"][0]["intent_code"] == "exchange_forex"
            assert snapshot["current_graph"]["status"] == "completed"

    asyncio.run(run())


def test_v2_proactive_recommendation_no_selection_returns_idle_without_graph() -> None:
    proactive_recommendation = {
        "introText": "工资到账后，这里有一组推荐事项。",
        "items": [
            {
                "recommendationItemId": "rec_transfer_mom",
                "intentCode": "transfer_money",
                "title": "给妈妈转账2000元",
                "slotMemory": {
                    "recipient_name": "妈妈",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last_four": "1234",
                    "amount": "2000",
                },
                "executionPayload": {},
                "allowDirectExecute": True,
            }
        ],
    }

    async def run() -> None:
        app, _ = _test_v2_app(
            recommendation_router=_StaticRecommendationRouter(
                ProactiveRecommendationRouteDecision(
                    route_mode=ProactiveRecommendationRouteMode.NO_SELECTION,
                    selectedRecommendationIds=[],
                    selectedIntents=[],
                    hasUserModification=False,
                    reason="用户明确表示不执行推荐事项",
                )
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "这些都不要",
                    "proactiveRecommendation": proactive_recommendation,
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            assert snapshot["current_graph"] is None
            assert snapshot["pending_graph"] is None
            assert snapshot["messages"][-1]["content"] == "好的，本次不执行这些推荐事项。"

    asyncio.run(run())


def test_v2_proactive_recommendation_hidden_balance_query_reuses_shared_account_context() -> None:
    proactive_recommendation = {
        "introText": "工资到账后，这里有几项待办。",
        "sharedSlotMemory": {
            "card_number": "6222000100001234567",
            "phone_last_four": "9999",
        },
        "items": [
            {
                "recommendationItemId": "rec-gas-bill",
                "intentCode": "pay_gas_bill",
                "title": "缴纳天然气费",
                "description": "已带入燃气户号和建议金额",
                "slotMemory": {
                    "gas_account_number": "88001234",
                    "amount": "88",
                },
                "executionPayload": {"provider": "city_gas"},
                "allowDirectExecute": True,
            },
            {
                "recommendationItemId": "rec-transfer-mom",
                "intentCode": "transfer_money",
                "title": "给妈妈转账2000元",
                "description": "已带入妈妈的收款信息",
                "slotMemory": {
                    "recipient_name": "妈妈",
                    "recipient_card_number": "6222020100049999999",
                    "recipient_phone_last_four": "9999",
                    "amount": "2000",
                },
                "executionPayload": {"currency": "CNY"},
                "allowDirectExecute": True,
            },
        ],
    }

    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_ExplodingRecognizer(),
            graph_builder=_ProactiveConditionalRepairGraphBuilder(),
            recommendation_router=_StaticRecommendationRouter(
                ProactiveRecommendationRouteDecision(
                    route_mode=ProactiveRecommendationRouteMode.INTERACTIVE_GRAPH,
                    selectedRecommendationIds=["rec-gas-bill", "rec-transfer-mom"],
                    selectedIntents=["pay_gas_bill", "transfer_money"],
                    hasUserModification=True,
                    modificationReasons=["用户新增了余额条件并修改转账金额"],
                    reason="需要重建带条件的执行图",
                )
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "我选择缴天然气费和转账，如果余额超过2000，那么就给我妈妈转3000",
                    "proactiveRecommendation": proactive_recommendation,
                },
            )
            assert first_turn.status_code == 200
            pending_graph = first_turn.json()["snapshot"]["pending_graph"]
            assert pending_graph is not None
            assert [node["intent_code"] for node in pending_graph["nodes"]] == [
                "pay_gas_bill",
                "query_account_balance",
                "transfer_money",
            ]
            assert pending_graph["nodes"][1]["slot_memory"] == {
                "card_number": "6222000100001234567",
                "phone_last_four": "9999",
            }

            confirm_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/actions",
                json={
                    "task_id": pending_graph["graph_id"],
                    "source": "router",
                    "action_code": "confirm_graph",
                    "confirm_token": pending_graph["confirm_token"],
                },
            )
            assert confirm_turn.status_code == 200
            snapshot = confirm_turn.json()["snapshot"]
            current_graph = snapshot["current_graph"]
            assert current_graph["status"] == "completed"
            assert [node["status"] for node in current_graph["nodes"]] == [
                "completed",
                "completed",
                "completed",
            ]
            assert all("请提供卡号" not in message["content"] for message in snapshot["messages"])
            assert snapshot["messages"][-1]["content"] == "已向我妈妈转账 3000 元，转账成功"

    asyncio.run(run())


def test_v2_router_pre_dispatch_gate_waits_before_dispatching_missing_slots() -> None:
    async def run() -> None:
        builder = _MissingSlotGraphBuilder()
        app, orchestrator = _test_v2_app(graph_builder=builder)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            first_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我缴天然气费"},
            )
            assert first_turn.status_code == 200
            snapshot = first_turn.json()["snapshot"]
            assert snapshot["current_graph"] is not None
            node = snapshot["current_graph"]["nodes"][0]
            assert node["status"] == "waiting_user_input"
            assert node["task_id"] is None
            session = orchestrator.session_store.get(session_id)
            assert not session.tasks

    asyncio.run(run())


def test_v2_router_completes_after_slots_are_filled() -> None:
    async def run() -> None:
        builder = _MissingSlotGraphBuilder()
        app, _ = _test_v2_app(graph_builder=builder)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "帮我缴天然气费"},
            )
            second_turn = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={"content": "给燃气户号88001234交88元"},
            )
            assert second_turn.status_code == 200
            snapshot = second_turn.json()["snapshot"]
            assert snapshot["current_graph"]["status"] == "completed"
            assert snapshot["messages"][-1]["content"] == "已为燃气户号 88001234 缴费 88 元"

    asyncio.run(run())


def test_v2_router_guided_selection_with_prefilled_slots_dispatches() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = (await client.post("/api/router/v2/sessions")).json()["session_id"]
            response = await client.post(
                f"/api/router/v2/sessions/{session_id}/messages",
                json={
                    "content": "帮我缴天然气费",
                    "guidedSelection": {
                        "selectedIntents": [
                            {
                                "intentCode": "pay_gas_bill",
                                "title": "缴纳天然气费",
                                "slotMemory": {
                                    "gas_account_number": "88001234",
                                    "amount": "88",
                                },
                            }
                        ]
                    },
                },
            )
            assert response.status_code == 200
            snapshot = response.json()["snapshot"]
            assert snapshot["current_graph"]["status"] == "completed"
            assert snapshot["messages"][-1]["content"] == "已为燃气户号 88001234 缴费 88 元"

    asyncio.run(run())
