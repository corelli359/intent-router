from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json

import httpx
import pytest
import sys
from pathlib import Path

from router_service.api.app import create_router_app
from router_service.api.dependencies import (
    get_event_broker,
    get_orchestrator,
)
from router_service.api.sse.broker import EventBroker


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.support.mock_agent_client import MockStreamingAgentClient
from router_service.core.support.agent_client import StreamingAgentClient
from router_service.core.shared.diagnostics import RouterDiagnosticCode, diagnostic
from router_service.core.shared.domain import AgentStreamChunk, IntentDefinition, IntentMatch, Task, TaskStatus
from router_service.core.recognition.recognizer import RecognitionResult
from router_service.core.shared.graph_domain import (
    ExecutionGraphState,
    GraphAction,
    GraphCondition,
    GraphEdge,
    GraphEdgeType,
    GraphNodeState,
    GraphNodeStatus,
    GraphStatus,
    ProactiveRecommendationPayload,
    ProactiveRecommendationRouteDecision,
    ProactiveRecommendationRouteMode,
    SlotBindingSource,
    SlotBindingState,
)
from router_service.core.graph.orchestrator import GraphRouterOrchestrator
from router_service.core.graph.planner import BasicTurnInterpreter, SequentialIntentGraphPlanner
from router_service.core.slots.understanding_validator import UnderstandingValidationResult


class _StaticCatalog:
    def __init__(self, intents: list[IntentDefinition]) -> None:
        self._intents = intents

    def list_active(self) -> list[IntentDefinition]:
        return list(self._intents)

    def active_intents_by_code(self) -> Mapping[str, IntentDefinition]:
        return {intent.intent_code: intent for intent in self._intents if not intent.is_fallback}

    def get_active_intent(self, intent_code: str) -> IntentDefinition | None:
        return self.active_intents_by_code().get(intent_code)

    def get_fallback_intent(self) -> IntentDefinition | None:
        return next((intent for intent in self._intents if intent.is_fallback), None)


def _parse_sse_frames(raw_text: str) -> list[tuple[str, str]]:
    frames: list[tuple[str, str]] = []
    for chunk in raw_text.split("\n\n"):
        event_name: str | None = None
        data_value: str | None = None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_value = line.split(":", 1)[1].strip()
        if event_name is not None and data_value is not None:
            frames.append((event_name, data_value))
    return frames


def _message_payloads(raw_text: str) -> list[dict[str, object]]:
    return [
        json.loads(data)
        for event, data in _parse_sse_frames(raw_text)
        if event == "message"
    ]


def _is_recognition_payload(payload: dict[str, object]) -> bool:
    return payload.get("stage") == "intent_recognition"


def _non_recognition_payloads(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    return [payload for payload in payloads if not _is_recognition_payload(payload)]


_ASSISTANT_PROTOCOL_OUTPUT_KEYS = {
    "ok",
    "current_task",
    "task_list",
    "status",
    "intent_code",
    "completion_state",
    "completion_reason",
    "message",
    "slot_memory",
    "output",
}
_ASSISTANT_PROTOCOL_OUTPUT_KEYS_WITH_AGENT = _ASSISTANT_PROTOCOL_OUTPUT_KEYS | {"agent_id"}


class _AsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _ag_trans_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="AG_TRANS",
        name="立即发起一笔转账交易",
        description="执行转账，需要收款人姓名和金额。",
        examples=["给小明转账", "我要转账"],
        keywords=["转账", "转钱", "汇款"],
        agent_id="AG_TRANS",
        agent_url="http://test-agent/ag_trans",
        dispatch_priority=100,
        primary_threshold=0.72,
        candidate_threshold=0.5,
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
                "label": "金额",
                "description": "当前转账金额",
                "value_type": "currency",
                "required": True,
            },
        ],
        request_schema={
            "type": "object",
            "required": ["session_id", "txt", "stream", "config_variables"],
        },
        field_mapping={
            "session_id": "$session.id",
            "txt": "$message.current",
            "stream": "true",
            "config_variables.custID": "$session.cust_id",
            "config_variables.currentDisplay": "",
            "config_variables.intent": "$intent",
            "config_variables.recent_messages": "$context.recent_messages",
            "config_variables.long_term_memory": "$context.long_term_memory",
            "config_variables.slots_data.amount": "$slot_memory.amount",
            "config_variables.slots_data.payer_card_no": "$slot_memory.payer_card_no",
            "config_variables.slots_data.payer_card_remark": "$slot_memory.payer_card_remark",
            "config_variables.slots_data.payee_name": "$slot_memory.payee_name",
            "config_variables.slots_data.payee_card_no": "$slot_memory.payee_card_no",
            "config_variables.slots_data.payee_card_remark": "$slot_memory.payee_card_remark",
            "config_variables.slots_data.payee_card_bank": "$slot_memory.payee_card_bank",
            "config_variables.slots_data.payee_phone": "$slot_memory.payee_phone",
        },
        graph_build_hints={"provides_context_keys": ["amount", "business_status"]},
    )


def _pay_gas_bill_intent() -> IntentDefinition:
    return next(intent for intent in _mock_intents() if intent.intent_code == "pay_gas_bill").model_copy(deep=True)


def _fallback_general_intent() -> IntentDefinition:
    return IntentDefinition(
        intent_code="FALLBACK_GENERAL",
        name="兜底智能体",
        description="处理业务智能体无法完成的事项。",
        examples=["帮我处理一下"],
        keywords=[],
        agent_url="http://test-agent/fallback",
        dispatch_priority=1,
        is_fallback=True,
    )


class _TransferOnlyRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del intents, recent_messages, long_term_memory, on_delta
        if "转账" in message:
            return RecognitionResult(
                primary=[IntentMatch(intent_code="AG_TRANS", confidence=0.97, reason="fixed transfer contract")],
                candidates=[],
            )
        return RecognitionResult(primary=[], candidates=[])


class _TransferThenPaymentRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del intents, recent_messages, long_term_memory, on_delta
        primary: list[IntentMatch] = []
        if "转账" in message:
            primary.append(IntentMatch(intent_code="AG_TRANS", confidence=0.97, reason="fixed transfer"))
        if "缴费" in message:
            primary.append(IntentMatch(intent_code="pay_gas_bill", confidence=0.94, reason="fixed payment"))
        return RecognitionResult(primary=primary, candidates=[])


class _RequestContextRecordingRecognizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recognize(
        self,
        message,
        intents,
        recent_messages,
        long_term_memory,
        recommend_task=None,
        on_delta=None,
    ):
        del intents, long_term_memory, on_delta
        self.calls.append(
            {
                "message": message,
                "recent_messages": list(recent_messages),
                "recommend_task": list(recommend_task or []),
            }
        )
        return RecognitionResult(
            primary=[IntentMatch(intent_code="AG_TRANS", confidence=0.97, reason="fixed transfer contract")],
            candidates=[],
        )


class _RecognizerFailureRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del message, intents, recent_messages, long_term_memory, on_delta
        return RecognitionResult(
            primary=[],
            candidates=[],
            diagnostics=[
                diagnostic(
                    RouterDiagnosticCode.RECOGNIZER_LLM_FAILED,
                    source="recognizer",
                    message="意图识别 LLM 失败，当前不执行本地兜底识别",
                    details={
                        "error_type": "ConnectError",
                        "error": "ConnectError: model backend unavailable",
                    },
                )
            ],
        )


@dataclass
class _ContractTransferUnderstandingValidator:
    def _bindings(self, slot_memory: dict[str, str], *, source_text: str) -> list[SlotBindingState]:
        return [
            SlotBindingState(
                slot_key=slot_key,
                value=value,
                source=SlotBindingSource.USER_MESSAGE,
                source_text=source_text,
                confidence=0.95,
            )
            for slot_key, value in slot_memory.items()
        ]

    async def validate_node(
        self,
        *,
        intent,
        node,
        graph_source_message,
        current_message,
        recent_messages=None,
        long_term_memory=None,
    ) -> UnderstandingValidationResult:
        del intent, graph_source_message, long_term_memory, recent_messages
        slot_memory = dict(node.slot_memory)
        if "小明" in current_message:
            slot_memory["payee_name"] = "小明"
        digits = "".join(character for character in current_message if character.isdigit())
        if digits:
            slot_memory["amount"] = digits
        missing_required_slots: list[str] = []
        if "amount" not in slot_memory:
            missing_required_slots.append("amount")
        if "payee_name" not in slot_memory:
            missing_required_slots.append("payee_name")
        prompt_message = {
            ("amount", "payee_name"): "请提供金额、收款人姓名",
            ("amount",): "请提供金额",
            ("payee_name",): "请提供收款人姓名",
        }.get(tuple(missing_required_slots))
        return UnderstandingValidationResult(
            slot_memory=slot_memory,
            slot_bindings=self._bindings(slot_memory, source_text=current_message),
            history_slot_keys=[],
            missing_required_slots=missing_required_slots,
            ambiguous_slot_keys=[],
            invalid_slot_keys=[],
            needs_confirmation=False,
            can_dispatch=not missing_required_slots,
            prompt_message=prompt_message,
            diagnostics=[],
        )


@dataclass
class _MultiTurnOverrideTransferUnderstandingValidator:
    payee_candidates: tuple[str, ...] = ("小明", "小刚", "小红", "王芳", "李雷", "妈妈", "弟弟")

    def _bindings(self, slot_memory: dict[str, str], *, source_text: str) -> list[SlotBindingState]:
        return [
            SlotBindingState(
                slot_key=slot_key,
                value=value,
                source=SlotBindingSource.USER_MESSAGE,
                source_text=source_text,
                confidence=0.95,
            )
            for slot_key, value in slot_memory.items()
        ]

    def _payee_name(self, current_message: str) -> str | None:
        for candidate in self.payee_candidates:
            if candidate in current_message:
                return candidate
        return None

    async def validate_node(
        self,
        *,
        intent,
        node,
        graph_source_message,
        current_message,
        recent_messages=None,
        long_term_memory=None,
    ) -> UnderstandingValidationResult:
        del intent, graph_source_message, recent_messages, long_term_memory
        slot_memory = dict(node.slot_memory)
        payee_name = self._payee_name(current_message)
        if payee_name is not None:
            slot_memory["payee_name"] = payee_name
        digits = "".join(character for character in current_message if character.isdigit())
        if digits:
            slot_memory["amount"] = digits
        missing_required_slots: list[str] = []
        if "amount" not in slot_memory:
            missing_required_slots.append("amount")
        if "payee_name" not in slot_memory:
            missing_required_slots.append("payee_name")
        prompt_message = {
            ("amount", "payee_name"): "请提供金额、收款人姓名",
            ("amount",): "请提供金额",
            ("payee_name",): "请提供收款人姓名",
        }.get(tuple(missing_required_slots))
        return UnderstandingValidationResult(
            slot_memory=slot_memory,
            slot_bindings=self._bindings(slot_memory, source_text=current_message),
            history_slot_keys=[],
            missing_required_slots=missing_required_slots,
            ambiguous_slot_keys=[],
            invalid_slot_keys=[],
            needs_confirmation=False,
            can_dispatch=not missing_required_slots,
            prompt_message=prompt_message,
            diagnostics=[],
        )


@dataclass
class _RouterOnlyTransferPaymentUnderstandingValidator:
    payee_candidates: tuple[str, ...] = ("杨丽敏",)

    def _bindings(self, slot_memory: dict[str, str], *, source_text: str) -> list[SlotBindingState]:
        return [
            SlotBindingState(
                slot_key=slot_key,
                value=value,
                source=SlotBindingSource.USER_MESSAGE,
                source_text=source_text,
                confidence=0.95,
            )
            for slot_key, value in slot_memory.items()
        ]

    def _digits(self, text: str) -> str:
        return "".join(character for character in text if character.isdigit())

    def _payee_name(self, current_message: str) -> str | None:
        for candidate in self.payee_candidates:
            if candidate in current_message:
                return candidate
        return None

    async def validate_node(
        self,
        *,
        intent,
        node,
        graph_source_message,
        current_message,
        recent_messages=None,
        long_term_memory=None,
    ) -> UnderstandingValidationResult:
        del graph_source_message, recent_messages, long_term_memory
        slot_memory = dict(node.slot_memory)
        intent_code = getattr(intent, "intent_code", "")
        if intent_code == "AG_TRANS":
            payee_name = self._payee_name(current_message)
            if payee_name is not None:
                slot_memory["payee_name"] = payee_name
            digits = self._digits(current_message)
            if digits:
                slot_memory["amount"] = digits
            required_slots = ("amount", "payee_name")
            prompt_by_missing = {
                ("amount", "payee_name"): "请提供金额、收款人姓名",
                ("amount",): "请提供金额",
                ("payee_name",): "请提供收款人姓名",
            }
        elif intent_code == "pay_gas_bill":
            digits = self._digits(current_message)
            if "户号" in current_message and digits:
                slot_memory["gas_account_number"] = digits
            elif "元" in current_message and digits:
                slot_memory["amount"] = digits
            required_slots = ("gas_account_number", "amount")
            prompt_by_missing = {
                ("gas_account_number", "amount"): "请提供燃气户号、缴费金额",
                ("gas_account_number",): "请提供燃气户号",
                ("amount",): "请提供缴费金额",
            }
        else:
            required_slots = ()
            prompt_by_missing = {}

        missing_required_slots = [
            slot_key
            for slot_key in required_slots
            if slot_key not in slot_memory
        ]
        prompt_message = prompt_by_missing.get(tuple(missing_required_slots))
        return UnderstandingValidationResult(
            slot_memory=slot_memory,
            slot_bindings=self._bindings(slot_memory, source_text=current_message),
            history_slot_keys=[],
            missing_required_slots=missing_required_slots,
            ambiguous_slot_keys=[],
            invalid_slot_keys=[],
            needs_confirmation=False,
            can_dispatch=not missing_required_slots,
            prompt_message=prompt_message,
            diagnostics=[],
        )


class _TrailingTerminalChunkAgentClient:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def stream(self, task: Task, user_input: str):
        del user_input
        self.tasks.append(task)
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content="转账已受理",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"phase": "accepted"},
        )
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content="转账成功",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"phase": "settled"},
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        del session_id, task_id, agent_url
        return None

    async def close(self) -> None:
        return None


class _EmptyHandoverThenFallbackAgentClient:
    def __init__(self) -> None:
        self.intent_codes: list[str] = []
        self.task_ids: list[str] = []

    async def stream(self, task: Task, user_input: str):
        del user_input
        self.intent_codes.append(task.intent_code)
        self.task_ids.append(task.task_id)
        if task.intent_code == "AG_TRANS":
            yield AgentStreamChunk(
                task_id=task.task_id,
                event="final",
                content="当前转账智能体无法完成该事项",
                ishandover=True,
                status=TaskStatus.COMPLETED,
                output={},
            )
            return
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content="兜底智能体已接管该事项",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            output={
                "message": "兜底智能体已接管该事项",
                "completion_state": 2,
                "completion_reason": "fallback_agent_done",
                "ishandover": True,
                "handOverReason": "business_agent_returned_empty_output",
                "data": [{"answer": "兜底智能体已接管该事项"}],
            },
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        del session_id, task_id, agent_url
        return None

    async def close(self) -> None:
        return None


def _assistant_protocol_ag_trans_intent() -> IntentDefinition:
    intent = _ag_trans_intent().model_copy(deep=True)
    intent.field_mapping = {
        "session_id": "$session.id",
        "txt": "$message.current",
        "stream": "true",
        "config_variables.custID": "$config_variables.custID",
        "config_variables.sessionID": "$config_variables.sessionID",
        "config_variables.currentDisplay": "$config_variables.currentDisplay",
        "config_variables.agentSessionID": "$config_variables.agentSessionID",
        "config_variables.slots_data.amount": "$slot_memory.amount",
        "config_variables.slots_data.payee_name": "$slot_memory.payee_name",
    }
    return intent


class _AssistantProtocolTransferAgentClient:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def stream(self, task: Task, user_input: str):
        del user_input
        self.tasks.append(task)
        amount = str(task.slot_memory.get("amount") or "")
        payee_name = str(task.slot_memory.get("payee_name") or "")
        payload = {
            "agent": "transfer_money",
            "amount": amount,
            "payee_name": payee_name,
            "business_status": "success",
        }
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已向{payee_name}转账 {amount} CNY，转账成功",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=payload,
            output={
                "message": f"已向{payee_name}转账 {amount} CNY，转账成功",
                "completion_state": 2,
                "completion_reason": "agent_final_done",
                "ishandover": True,
                "handOverReason": "已提供收款人和金额交易对象",
                "data": [
                    {
                        "isSubAgent": "True",
                        "typIntent": "mbpTransfer",
                        "answer": f"||{amount}|{payee_name}|",
                    }
                ],
                "event": "final",
                "payload": payload,
            },
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        del session_id, task_id, agent_url
        return None

    async def close(self) -> None:
        return None


class _AssistantProtocolWorkflowAgentClient:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def stream(self, task: Task, user_input: str):
        del user_input
        self.tasks.append(task)
        amount = str(task.slot_memory.get("amount") or "")
        payee_name = str(task.slot_memory.get("payee_name") or "")
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="message",
            content="收款人校验通过",
            ishandover=False,
            status=TaskStatus.RUNNING,
            payload={"workflow_node": "validate_payee"},
            output={
                "node_id": "validate_payee",
                "event": "message",
                "message": "收款人校验通过",
                "ishandover": False,
                "data": [{"answer": "收款人校验通过"}],
                "payload": {"workflow_node": "validate_payee"},
            },
        )
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已向{payee_name}转账 {amount} CNY，转账成功",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload={"workflow_node": "execute_transfer"},
            output={
                "node_id": "execute_transfer",
                "event": "final",
                "message": f"已向{payee_name}转账 {amount} CNY，转账成功",
                "completion_state": 2,
                "completion_reason": "agent_final_done",
                "ishandover": True,
                "handOverReason": "已提供收款人和金额交易对象",
                "data": [
                    {
                        "isSubAgent": "True",
                        "typIntent": "mbpTransfer",
                        "answer": f"||{amount}|{payee_name}|",
                    }
                ],
                "payload": {"workflow_node": "execute_transfer"},
            },
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        del session_id, task_id, agent_url
        return None

    async def close(self) -> None:
        return None


class _AssistantProtocolPartialCompletionAgentClient:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def stream(self, task: Task, user_input: str):
        del user_input
        self.tasks.append(task)
        amount = str(task.slot_memory.get("amount") or "")
        payee_name = str(task.slot_memory.get("payee_name") or "")
        payload = {
            "agent": "transfer_money",
            "amount": amount,
            "payee_name": payee_name,
            "business_status": "accepted",
        }
        yield AgentStreamChunk(
            task_id=task.task_id,
            event="final",
            content=f"已受理向{payee_name}转账 {amount} CNY，等待助手确认完成态",
            ishandover=True,
            status=TaskStatus.COMPLETED,
            payload=payload,
            output={
                "message": f"已受理向{payee_name}转账 {amount} CNY，等待助手确认完成态",
                "completion_state": 1,
                "ishandover": True,
                "handOverReason": "等待助手确认完成态",
                "data": [
                    {
                        "isSubAgent": "True",
                        "typIntent": "mbpTransfer",
                        "answer": f"||{amount}|{payee_name}|",
                    }
                ],
                "event": "final",
                "payload": payload,
            },
        )

    async def cancel(self, session_id: str, task_id: str, agent_url: str | None = None) -> None:
        del session_id, task_id, agent_url
        return None

    async def close(self) -> None:
        return None


@dataclass(frozen=True)
class _RouterOnlyTurnExpectation:
    user_input: str
    graph_status: str
    node_status: str
    slot_memory: dict[str, str]
    assistant_reply: str


@dataclass(frozen=True)
class _RouterOnlyContractScenario:
    name: str
    turns: tuple[_RouterOnlyTurnExpectation, ...]
    final_shared_slot_memory: dict[str, str]


_TRANSFER_ROUTER_ONLY_CONTRACT_SCENARIOS = (
    _RouterOnlyContractScenario(
        name="named_first_turn_then_amount",
        turns=(
            _RouterOnlyTurnExpectation(
                user_input="给小明转账",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={"payee_name": "小明"},
                assistant_reply="请提供金额",
            ),
            _RouterOnlyTurnExpectation(
                user_input="200",
                graph_status="ready_for_dispatch",
                node_status="ready_for_dispatch",
                slot_memory={"payee_name": "小明", "amount": "200"},
                assistant_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_shared_slot_memory={"payee_name": "小明", "amount": "200"},
    ),
    _RouterOnlyContractScenario(
        name="generic_then_name_then_amount",
        turns=(
            _RouterOnlyTurnExpectation(
                user_input="我要转账",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={},
                assistant_reply="请提供金额、收款人姓名",
            ),
            _RouterOnlyTurnExpectation(
                user_input="给小明",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={"payee_name": "小明"},
                assistant_reply="请提供金额",
            ),
            _RouterOnlyTurnExpectation(
                user_input="200",
                graph_status="ready_for_dispatch",
                node_status="ready_for_dispatch",
                slot_memory={"payee_name": "小明", "amount": "200"},
                assistant_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_shared_slot_memory={"payee_name": "小明", "amount": "200"},
    ),
    _RouterOnlyContractScenario(
        name="generic_then_amount_then_name",
        turns=(
            _RouterOnlyTurnExpectation(
                user_input="我要转账",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={},
                assistant_reply="请提供金额、收款人姓名",
            ),
            _RouterOnlyTurnExpectation(
                user_input="200",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={"amount": "200"},
                assistant_reply="请提供收款人姓名",
            ),
            _RouterOnlyTurnExpectation(
                user_input="给小明",
                graph_status="ready_for_dispatch",
                node_status="ready_for_dispatch",
                slot_memory={"amount": "200", "payee_name": "小明"},
                assistant_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_shared_slot_memory={"payee_name": "小明", "amount": "200"},
    ),
    _RouterOnlyContractScenario(
        name="all_slots_in_single_turn",
        turns=(
            _RouterOnlyTurnExpectation(
                user_input="给小明转账200元",
                graph_status="ready_for_dispatch",
                node_status="ready_for_dispatch",
                slot_memory={"payee_name": "小明", "amount": "200"},
                assistant_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_shared_slot_memory={"payee_name": "小明", "amount": "200"},
    ),
    _RouterOnlyContractScenario(
        name="first_turn_has_name_only_variant",
        turns=(
            _RouterOnlyTurnExpectation(
                user_input="我要给小明转账",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={"payee_name": "小明"},
                assistant_reply="请提供金额",
            ),
            _RouterOnlyTurnExpectation(
                user_input="200",
                graph_status="ready_for_dispatch",
                node_status="ready_for_dispatch",
                slot_memory={"payee_name": "小明", "amount": "200"},
                assistant_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_shared_slot_memory={"payee_name": "小明", "amount": "200"},
    ),
    _RouterOnlyContractScenario(
        name="first_turn_has_amount_only_variant",
        turns=(
            _RouterOnlyTurnExpectation(
                user_input="我要转账200元",
                graph_status="waiting_user_input",
                node_status="waiting_user_input",
                slot_memory={"amount": "200"},
                assistant_reply="请提供收款人姓名",
            ),
            _RouterOnlyTurnExpectation(
                user_input="给小明",
                graph_status="ready_for_dispatch",
                node_status="ready_for_dispatch",
                slot_memory={"amount": "200", "payee_name": "小明"},
                assistant_reply="路由识别完成：事项「立即发起一笔转账交易」已具备执行条件，当前为 router_only 模式，未调用执行 agent",
            ),
        ),
        final_shared_slot_memory={"payee_name": "小明", "amount": "200"},
    ),
)


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


class _MisorderedRecognitionRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del message, intents, recent_messages, long_term_memory, on_delta
        return RecognitionResult(
            primary=[
                IntentMatch(intent_code="pay_gas_bill", confidence=0.94, reason="raw recognizer order"),
                IntentMatch(intent_code="transfer_money", confidence=0.97, reason="raw recognizer order"),
            ],
            candidates=[],
        )


class _GraphCardOrderPlanner:
    async def plan(self, *, message, matches, intents_by_code, recent_messages=None, long_term_memory=None):
        del matches, intents_by_code, recent_messages, long_term_memory
        graph = ExecutionGraphState(
            source_message=message,
            summary="按执行图顺序先转账再缴费",
            status=GraphStatus.DRAFT,
        )
        transfer = GraphNodeState(
            intent_code="transfer_money",
            title="给小明转账500元",
            confidence=0.97,
            position=0,
            source_fragment="给小明转账500元",
            slot_memory={"recipient_name": "小明", "amount": "500"},
        )
        gas = GraphNodeState(
            intent_code="pay_gas_bill",
            title="缴燃气费88元",
            confidence=0.94,
            position=1,
            source_fragment="缴燃气费88元",
            slot_memory={"gas_account_number": "88001234", "amount": "88"},
        )
        gas.depends_on.append(transfer.node_id)
        gas.relation_reason = "转账后再缴燃气费"
        graph.nodes.extend([transfer, gas])
        graph.edges.append(
            GraphEdge(
                source_node_id=transfer.node_id,
                target_node_id=gas.node_id,
                relation_type=GraphEdgeType.SEQUENTIAL,
                label="转账后再缴燃气费",
            )
        )
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


class _DirectTransferRecognizer:
    async def recognize(self, message, intents, recent_messages, long_term_memory, on_delta=None):
        del intents, recent_messages, long_term_memory, on_delta
        assert message == "给小红转200"
        return RecognitionResult(
            primary=[IntentMatch(intent_code="transfer_money", confidence=0.98, reason="fixed")],
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
    intents: list[IntentDefinition] | None = None,
    understanding_validator=None,
    agent_client=None,
) -> tuple[object, GraphRouterOrchestrator]:
    broker = EventBroker()
    orchestrator = GraphRouterOrchestrator(
        publish_event=broker.publish,
        intent_catalog=_StaticCatalog(intents or _mock_intents()),
        recognizer=recognizer or _MessageRecognizer(),
        graph_builder=graph_builder,
        planner=planner or SequentialIntentGraphPlanner(),
        turn_interpreter=turn_interpreter or BasicTurnInterpreter(),
        recommendation_router=recommendation_router,
        agent_client=agent_client or MockStreamingAgentClient(),
        understanding_validator=understanding_validator,
    )
    app = create_router_app()
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_event_broker] = lambda: broker
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


def test_v1_message_router_only_keeps_latest_payee_across_multiple_waiting_turns() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            intents=[_ag_trans_intent()],
            recognizer=_TransferOnlyRecognizer(),
            understanding_validator=_MultiTurnOverrideTransferUnderstandingValidator(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "router_only_latest_payee_demo"

            first_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "我要转账",
                    "stream": False,
                    "executionMode": "router_only",
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert first_turn.status_code == 200
            first_body = first_turn.json()
            assert first_body["status"] == "waiting_user_input"
            assert first_body["slot_memory"] == {}
            assert first_body["message"] == "请提供金额、收款人姓名"

            second_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "小刚",
                    "stream": False,
                    "executionMode": "router_only",
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert second_turn.status_code == 200
            second_body = second_turn.json()
            assert second_body["status"] == "waiting_user_input"
            assert second_body["slot_memory"] == {
                "payee_name": "小刚",
            }
            assert second_body["message"] == "请提供金额"

            third_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "小红吧",
                    "stream": False,
                    "executionMode": "router_only",
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert third_turn.status_code == 200
            third_body = third_turn.json()
            assert third_body["status"] == "waiting_user_input"
            assert third_body["slot_memory"] == {
                "payee_name": "小红",
            }
            assert third_body["message"] == "请提供金额"

            fourth_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "200",
                    "stream": False,
                    "executionMode": "router_only",
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_confirm_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert fourth_turn.status_code == 200
            fourth_body = fourth_turn.json()
            assert fourth_body["status"] == "ready_for_dispatch"
            assert fourth_body["completion_reason"] == "router_ready_for_dispatch"
            assert fourth_body["slot_memory"] == {
                "payee_name": "小红",
                "amount": "200",
            }
            assert fourth_body["output"]["ishandover"] is True
            assert fourth_body["output"]["handOverReason"] == "router_only_ready_for_dispatch"
            assert fourth_body["output"]["message"].startswith("Router 已完成识别和槽位校验")

    asyncio.run(run())


def test_v1_message_stream_router_only_ready_marks_handover() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            intents=[_ag_trans_intent()],
            recognizer=_TransferOnlyRecognizer(),
            understanding_validator=_ContractTransferUnderstandingValidator(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "router_only_stream_handover_demo"
            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小明转账200元",
                    "stream": True,
                    "executionMode": "router_only",
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        state_payloads = _non_recognition_payloads(_message_payloads(stream_text))
        assert len(state_payloads) == 1
        assert state_payloads[0]["status"] == "ready_for_dispatch"
        assert state_payloads[0]["completion_reason"] == "router_ready_for_dispatch"
        assert state_payloads[0]["output"]["ishandover"] is True
        assert state_payloads[0]["output"]["handOverReason"] == "router_only_ready_for_dispatch"
        assert state_payloads[0]["output"]["message"].startswith("Router 已完成识别和槽位校验")

    asyncio.run(run())


def test_v1_task_completion_stream_waits_for_continue_before_next_router_only_task() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            intents=[_ag_trans_intent(), _pay_gas_bill_intent()],
            recognizer=_TransferThenPaymentRecognizer(),
            understanding_validator=_RouterOnlyTransferPaymentUnderstandingValidator(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "router_only_completion_handover_demo"
            config_variables = [
                {"name": "custID", "value": "C0001"},
                {"name": "sessionID", "value": session_id},
                {"name": "agentSessionID", "value": session_id},
            ]

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "我要先转账,然后进行缴费",
                    "stream": True,
                    "executionMode": "router_only",
                    "config_variables": config_variables,
                },
            ) as response:
                first_stream = "".join([chunk async for chunk in response.aiter_text()])

            first_payloads = _non_recognition_payloads(_message_payloads(first_stream))
            assert first_payloads[-1]["status"] == "waiting_user_input"
            assert first_payloads[-1]["intent_code"] == "AG_TRANS"
            assert first_payloads[-1]["message"] == "请提供金额、收款人姓名"

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给杨丽敏转500元",
                    "stream": True,
                    "executionMode": "router_only",
                    "config_variables": config_variables,
                },
            ) as response:
                ready_stream = "".join([chunk async for chunk in response.aiter_text()])

            ready_payloads = _non_recognition_payloads(_message_payloads(ready_stream))
            ready_payload = ready_payloads[-1]
            current_task = ready_payload["current_task"]
            assert ready_payload["status"] == "ready_for_dispatch"
            assert ready_payload["intent_code"] == "AG_TRANS"
            assert ready_payload["slot_memory"] == {"payee_name": "杨丽敏", "amount": "500"}
            assert ready_payload["output"]["ishandover"] is True

            async with client.stream(
                "POST",
                "/api/v1/task/completion",
                json={
                    "sessionId": session_id,
                    "taskId": current_task,
                    "completionSignal": 2,
                    "stream": True,
                },
            ) as response:
                completion_stream = "".join([chunk async for chunk in response.aiter_text()])

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "继续",
                    "stream": True,
                    "executionMode": "router_only",
                    "config_variables": config_variables,
                },
            ) as response:
                continue_stream = "".join([chunk async for chunk in response.aiter_text()])

        assert _parse_sse_frames(completion_stream)[-1] == ("done", "[DONE]")
        completion_payloads = _message_payloads(completion_stream)
        assert completion_payloads
        assert all(payload.get("ok") is not False for payload in completion_payloads)
        assert all(payload.get("errorCode") != "ROUTER_TASK_NOT_FOUND" for payload in completion_payloads)
        completion_payload = completion_payloads[-1]
        assert completion_payload["status"] == "completed"
        assert completion_payload["intent_code"] == "AG_TRANS"
        assert completion_payload["current_task"] == current_task
        assert completion_payload["task_list"][0] == {"name": current_task, "status": "completed"}
        assert completion_payload["task_list"][1]["status"] == "waiting"

        assert _parse_sse_frames(continue_stream)[-1] == ("done", "[DONE]")
        continue_payloads = _non_recognition_payloads(_message_payloads(continue_stream))
        assert continue_payloads
        final_payload = continue_payloads[-1]
        assert final_payload["status"] == "waiting_user_input"
        assert final_payload["intent_code"] == "pay_gas_bill"
        assert final_payload["current_task"] != current_task
        assert final_payload["message"] == "请提供燃气户号、缴费金额"
        assert final_payload["task_list"][0] == {"name": current_task, "status": "completed"}
        assert final_payload["task_list"][1]["status"] == "waiting"

    asyncio.run(run())


def test_v1_message_empty_handover_output_routes_to_fallback_agent() -> None:
    async def run() -> None:
        agent_client = _EmptyHandoverThenFallbackAgentClient()
        app, _ = _test_v2_app(
            intents=[_ag_trans_intent(), _fallback_general_intent()],
            recognizer=_TransferOnlyRecognizer(),
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "empty_handover_routes_fallback_demo",
                    "txt": "给小明转账200元",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "empty_handover_routes_fallback_demo"},
                    ],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["status"] == "waiting_assistant_completion"
        assert body["completion_state"] == 1
        assert body["completion_reason"] == "assistant_confirmation_required"
        assert body["intent_code"] == "FALLBACK_GENERAL"
        assert body["current_task"].startswith("task_")
        assert body["task_list"] == [{"name": body["current_task"], "status": "waiting"}]
        assert body["output"]["message"] == "兜底智能体已接管该事项"
        assert body["output"]["handOverReason"] == "business_agent_returned_empty_output"
        assert agent_client.intent_codes == ["AG_TRANS", "FALLBACK_GENERAL"]
        assert len(set(agent_client.task_ids)) == 1

    asyncio.run(run())


def test_v1_message_stream_empty_handover_output_routes_to_fallback_agent() -> None:
    async def run() -> None:
        agent_client = _EmptyHandoverThenFallbackAgentClient()
        app, _ = _test_v2_app(
            intents=[_ag_trans_intent(), _fallback_general_intent()],
            recognizer=_TransferOnlyRecognizer(),
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": "stream_empty_handover_routes_fallback_demo",
                    "txt": "给小明转账200元",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "stream_empty_handover_routes_fallback_demo"},
                    ],
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        state_payloads = _non_recognition_payloads(_message_payloads(stream_text))
        assert state_payloads[-1]["status"] == "waiting_assistant_completion"
        assert state_payloads[-1]["intent_code"] == "FALLBACK_GENERAL"
        assert state_payloads[-1]["output"]["message"] == "兜底智能体已接管该事项"
        assert state_payloads[-1]["output"]["handOverReason"] == "business_agent_returned_empty_output"
        assert agent_client.intent_codes == ["AG_TRANS", "FALLBACK_GENERAL"]
        assert len(set(agent_client.task_ids)) == 1

    asyncio.run(run())


def test_v2_ag_trans_consumes_full_agent_stream_after_first_terminal_chunk() -> None:
    async def run() -> None:
        agent_client = _TrailingTerminalChunkAgentClient()
        app, orchestrator = _test_v2_app(
            intents=[_ag_trans_intent()],
            recognizer=_TransferOnlyRecognizer(),
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_trailing_terminal_demo"
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小明转账200元",
                    "stream": False,
                },
            )
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "waiting_assistant_completion"
            assert body["intent_code"] == "AG_TRANS"
            session = orchestrator.session_store.get(session_id)
            assistant_messages = [message.content for message in session.messages if message.role == "assistant"]
            assert assistant_messages[-2:] == ["转账已受理", "转账成功"]

            assert len(agent_client.tasks) == 1
            task = agent_client.tasks[0]
            assert task.agent_url == "http://test-agent/ag_trans"
            assert task.request_schema["required"] == [
                "session_id",
                "txt",
                "stream",
                "config_variables",
            ]
            assert task.field_mapping["session_id"] == "$session.id"
            assert task.field_mapping["txt"] == "$message.current"
            assert task.field_mapping["stream"] == "true"
            assert task.field_mapping["config_variables.intent"] == "$intent"
            assert task.field_mapping["config_variables.recent_messages"] == "$context.recent_messages"
            assert task.field_mapping["config_variables.long_term_memory"] == "$context.long_term_memory"
            assert task.field_mapping["config_variables.slots_data.amount"] == "$slot_memory.amount"
            assert task.field_mapping["config_variables.slots_data.payee_name"] == "$slot_memory.payee_name"

    asyncio.run(run())


def test_v2_router_message_assistant_protocol_waiting_response_for_missing_slots() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "assistant_missing_slots_demo",
                    "txt": "给小明转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_missing_slots_demo"},
                        {"name": "currentDisplay", "value": "display_001"},
                        {"name": "agentSessionID", "value": "assistant_missing_slots_demo"},
                    ],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert "snapshot" not in body
        assert body["intent_code"] == "AG_TRANS"
        assert body["status"] == "waiting_user_input"
        assert body["completion_state"] == 0
        assert body["completion_reason"] == "router_waiting_user_input"
        assert body["message"] == "请提供金额"
        assert body["slot_memory"] == {"payee_name": "小明"}
        assert body["current_task"].startswith("task_")
        assert body["task_list"] == [{"name": body["current_task"], "status": "waiting"}]
        assert body["output"] == {}
        assert agent_client.tasks == []

    asyncio.run(run())


def test_v2_router_message_assistant_protocol_uses_unique_task_name_before_agent_dispatch() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_MultiTurnOverrideTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_unique_pre_task_name_demo"
            first_waiting = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小明转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            )
            first_ready = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "200",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            )
            completion = await client.post(
                "/api/v1/task/completion",
                json={
                    "sessionId": session_id,
                    "taskId": first_ready.json()["current_task"],
                    "completionSignal": 2,
                    "stream": False,
                },
            )
            second_waiting = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小红转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            )

        assert first_waiting.status_code == 200
        assert first_ready.status_code == 200
        assert completion.status_code == 200
        assert second_waiting.status_code == 200
        first_waiting_body = first_waiting.json()
        second_waiting_body = second_waiting.json()
        assert first_waiting_body["status"] == "waiting_user_input"
        assert second_waiting_body["status"] == "waiting_user_input"
        assert first_waiting_body["current_task"].startswith("task_")
        assert second_waiting_body["current_task"].startswith("task_")
        assert first_waiting_body["current_task"] != second_waiting_body["current_task"]
        assert first_waiting_body["task_list"] == [
            {"name": first_waiting_body["current_task"], "status": "waiting"}
        ]
        assert second_waiting_body["task_list"] == [
            {"name": second_waiting_body["current_task"], "status": "waiting"}
        ]
        assert first_ready.json()["current_task"] == first_waiting_body["current_task"]

    asyncio.run(run())


def test_v2_router_message_assistant_protocol_returns_output_after_second_turn() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_second_turn_demo"
            first_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小明转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "display_001"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert first_turn.status_code == 200

            second_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "200",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "display_002"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )

        assert second_turn.status_code == 200
        body = second_turn.json()
        assert body["ok"] is True
        assert body["intent_code"] == "AG_TRANS"
        assert body["status"] == "waiting_assistant_completion"
        assert body["completion_state"] == 1
        assert body["completion_reason"] == "assistant_confirmation_required"
        assert body["agent_id"] == "AG_TRANS"
        assert body["message"] == "执行图等待助手确认完成态"
        assert _ASSISTANT_PROTOCOL_OUTPUT_KEYS.issubset(body)
        assert body.get("errorCode") is None
        assert body.get("stage") is None
        assert body.get("details") is None
        assert body["slot_memory"] == {
            "amount": "200",
            "payee_name": "小明",
        }
        assert body["current_task"].startswith("task_")
        assert body["task_list"] == [{"name": body["current_task"], "status": "waiting"}]
        assert body["output"]["data"] == [
            {
                "isSubAgent": "True",
                "typIntent": "mbpTransfer",
                "answer": "||200|小明|",
            }
        ]
        assert body["output"]["message"] == "已向小明转账 200 CNY，转账成功"
        assert body["output"]["completion_state"] == 2
        assert body["output"]["completion_reason"] == "agent_final_done"
        assert body["output"]["ishandover"] is True
        assert body["output"]["handOverReason"] == "已提供收款人和金额交易对象"
        assert body["output"]["payload"] == {
            "agent": "transfer_money",
            "amount": "200",
            "payee_name": "小明",
            "business_status": "success",
        }
        assert len(agent_client.tasks) == 1
        task = agent_client.tasks[0]
        assert task.input_context["config_variables"] == {
            "custID": "C0001",
            "sessionID": session_id,
            "currentDisplay": "display_002",
            "agentSessionID": session_id,
        }

    asyncio.run(run())


def test_v2_message_uses_recommend_task_and_current_display_only_for_router_context() -> None:
    async def run() -> None:
        recognizer = _RequestContextRecordingRecognizer()
        agent_client = _AssistantProtocolTransferAgentClient()
        app, _ = _test_v2_app(
            recognizer=recognizer,
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        recommend_task = [
            {
                "intentCode": "AG_TRANS",
                "title": "给小明转账200",
                "slotMemory": {"payee_name": "小明", "amount": "200"},
            }
        ]
        current_display = [
            {"role": "assistant", "content": "推荐事项：给小明转账200"},
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "router_only_context_demo",
                    "txt": "给小明转账200",
                    "stream": False,
                    "recommendTask": recommend_task,
                    "currentDisplay": current_display,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "router_only_context_demo"},
                        {"name": "currentDisplay", "value": "agent_display_passthrough"},
                        {"name": "agentSessionID", "value": "router_only_context_demo"},
                    ],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "waiting_assistant_completion"
        assert recognizer.calls == [
            {
                "message": "给小明转账200",
                "recent_messages": [
                    "[CURRENT_DISPLAY] assistant: 推荐事项：给小明转账200",
                    '[RECOMMEND_TASK] 1: {"intentCode":"AG_TRANS","slotMemory":{"amount":"200","payee_name":"小明"},"title":"给小明转账200"}',
                ],
                "recommend_task": recommend_task,
            }
        ]
        assert len(agent_client.tasks) == 1
        task_context = agent_client.tasks[0].input_context
        assert "recommend_task" not in task_context
        assert task_context["recent_messages"] == ["user: 给小明转账200"]
        assert task_context["config_variables"]["currentDisplay"] == "agent_display_passthrough"

    asyncio.run(run())


def test_v1_task_completion_with_real_router_app_returns_completed_output() -> None:
    async def run() -> None:
        app, orchestrator = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=_AssistantProtocolTransferAgentClient(),
        )
        session = orchestrator.session_store.create(cust_id="C0001", session_id="assistant_completion_demo")
        graph = ExecutionGraphState(
            source_message="给小明转账",
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
        session.attach_business(graph, router_only_mode=False, pending=False)
        task = Task(
            session_id=session.session_id,
            intent_code="AG_TRANS",
            agent_url="http://test-agent/ag_trans",
            intent_name="转账",
            intent_description="执行转账",
            confidence=0.96,
            status=TaskStatus.WAITING_USER_INPUT,
            slot_memory={"payee_name": "小明", "amount": "200"},
        )
        task.touch(TaskStatus.WAITING_USER_INPUT)
        session.tasks.append(task)
        node.task_id = task.task_id

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/task/completion",
                json={
                    "sessionId": session.session_id,
                    "taskId": task.task_id,
                    "completionSignal": 2,
                    "stream": False,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["current_task"] == task.task_id
        assert body["task_list"] == [{"name": task.task_id, "status": "completed"}]
        assert body["completion_state"] == 2
        assert body["completion_reason"] == "assistant_final_done"
        assert body["intent_code"] == "AG_TRANS"
        assert body["status"] == "completed"
        assert body["message"] == "执行图已完成"
        assert body["slot_memory"] == {"payee_name": "小明", "amount": "200"}
        assert body["output"] == {}

        session_after = orchestrator.session_store.get(session.session_id)
        assert session_after.current_graph is None
        assert session_after.pending_graph is None
        assert session_after.tasks == []
        assert session_after.shared_slot_memory == {
            "payee_name": "小明",
            "amount": "200",
        }

    asyncio.run(run())


def test_v1_task_completion_real_chain_waits_for_assistant_then_joins_to_completed() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolPartialCompletionAgentClient()
        app, orchestrator = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            first_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "assistant_partial_completion_demo",
                    "txt": "给小明转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_partial_completion_demo"},
                        {"name": "currentDisplay", "value": "display_001"},
                        {"name": "agentSessionID", "value": "assistant_partial_completion_demo"},
                    ],
                },
            )
            assert first_turn.status_code == 200

            second_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "assistant_partial_completion_demo",
                    "txt": "200",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_partial_completion_demo"},
                        {"name": "currentDisplay", "value": "display_002"},
                        {"name": "agentSessionID", "value": "assistant_partial_completion_demo"},
                    ],
                },
            )
            assert second_turn.status_code == 200
            second_body = second_turn.json()
            second_output = second_body

            completion = await client.post(
                "/api/v1/task/completion",
                json={
                    "sessionId": "assistant_partial_completion_demo",
                    "taskId": second_output["current_task"],
                    "completionSignal": 2,
                    "stream": False,
                },
            )

        assert second_body["ok"] is True
        assert second_output["current_task"].startswith("task_")
        assert second_output["task_list"] == [{"name": second_output["current_task"], "status": "waiting"}]
        assert second_output["status"] == "waiting_assistant_completion"
        assert second_output["completion_state"] == 1
        assert second_output["completion_reason"] == "assistant_confirmation_required"
        assert second_output["message"] == "执行图等待助手确认完成态"
        assert second_output["slot_memory"] == {"amount": "200", "payee_name": "小明"}
        assert second_output["output"]["completion_state"] == 1
        assert second_output["output"]["ishandover"] is True
        assert second_output["output"]["handOverReason"] == "等待助手确认完成态"
        assert second_output["output"]["payload"] == {
            "agent": "transfer_money",
            "amount": "200",
            "payee_name": "小明",
            "business_status": "accepted",
        }

        assert completion.status_code == 200
        completion_body = completion.json()
        completion_output = completion_body
        assert completion_body["ok"] is True
        assert completion_output["current_task"] == second_output["current_task"]
        assert completion_output["task_list"] == [{"name": second_output["current_task"], "status": "completed"}]
        assert completion_output["status"] == "completed"
        assert completion_output["completion_state"] == 2
        assert completion_output["completion_reason"] == "assistant_final_done"
        assert completion_output["message"] == "执行图已完成"
        assert completion_output["slot_memory"] == {"amount": "200", "payee_name": "小明"}
        assert completion_output["output"]["completion_state"] == 1

        session_after = orchestrator.session_store.get("assistant_partial_completion_demo")
        assert session_after.current_graph is None
        assert session_after.pending_graph is None
        assert session_after.tasks == []
        assert session_after.shared_slot_memory == {
            "payee_name": "小明",
            "amount": "200",
        }

    asyncio.run(run())


def test_v1_task_completion_stream_confirms_current_task_then_continue_runs_next_task() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        app, orchestrator = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        graph = ExecutionGraphState(
            source_message="给小红转300",
            summary="two transfers",
            status=GraphStatus.WAITING_ASSISTANT_COMPLETION,
        )
        first_node = GraphNodeState(
            intent_code="AG_TRANS",
            title="给小明转账",
            confidence=0.96,
            position=0,
            status=GraphNodeStatus.WAITING_ASSISTANT_COMPLETION,
            source_fragment="给小明转200",
            slot_memory={"payee_name": "小明", "amount": "200"},
        )
        first_node._agent_output = {
            "message": "已向小明转账 200 CNY，转账成功",
            "completion_state": 2,
            "completion_reason": "agent_final_done",
            "ishandover": True,
            "data": [
                {
                    "isSubAgent": "True",
                    "typIntent": "mbpTransfer",
                    "answer": "||200|小明|",
                }
            ],
        }
        second_node = GraphNodeState(
            intent_code="AG_TRANS",
            title="给小红转账",
            confidence=0.95,
            position=1,
            status=GraphNodeStatus.BLOCKED,
            source_fragment="给小红转300",
            depends_on=[first_node.node_id],
            slot_memory={"payee_name": "小红", "amount": "300"},
        )
        graph.nodes.extend([first_node, second_node])
        graph.edges.append(
            GraphEdge(
                source_node_id=first_node.node_id,
                target_node_id=second_node.node_id,
                relation_type=GraphEdgeType.SEQUENTIAL,
                label="按识别顺序执行",
            )
        )
        session = orchestrator.session_store.create(
            cust_id="C0001",
            session_id="assistant_completion_stream_multitask_demo",
        )
        session.attach_business(graph, router_only_mode=False, pending=False)
        first_task = Task(
            session_id=session.session_id,
            intent_code="AG_TRANS",
            agent_url="http://test-agent/ag_trans",
            intent_name="转账",
            intent_description="执行转账",
            confidence=0.96,
            status=TaskStatus.WAITING_ASSISTANT_COMPLETION,
            slot_memory={"payee_name": "小明", "amount": "200"},
        )
        first_task.touch(TaskStatus.WAITING_ASSISTANT_COMPLETION)
        session.tasks.append(first_task)
        first_node.task_id = first_task.task_id

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            async with client.stream(
                "POST",
                "/api/v1/task/completion",
                json={
                    "sessionId": session.session_id,
                    "taskId": first_task.task_id,
                    "completionSignal": 2,
                    "stream": True,
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session.session_id,
                    "txt": "继续",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session.session_id},
                    ],
                },
            ) as response:
                continue_stream_text = "".join([chunk async for chunk in response.aiter_text()])

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        message_payloads = [json.loads(data) for event, data in frames if event == "message"]
        assert len(message_payloads) == 1

        completed_payload = message_payloads[0]
        assert completed_payload["current_task"] == first_task.task_id
        assert completed_payload["status"] == "completed"
        assert completed_payload["completion_state"] == 2
        assert completed_payload["completion_reason"] == "assistant_final_done"
        assert completed_payload["output"]["message"] == "已向小明转账 200 CNY，转账成功"

        continue_frames = _parse_sse_frames(continue_stream_text)
        assert continue_frames[-1] == ("done", "[DONE]")
        continue_payloads = _non_recognition_payloads(
            [json.loads(data) for event, data in continue_frames if event == "message"]
        )
        assert continue_payloads
        next_payload = continue_payloads[-1]
        assert next_payload["current_task"].startswith("task_")
        assert next_payload["current_task"] != first_task.task_id
        assert next_payload["status"] == "waiting_assistant_completion"
        assert next_payload["completion_state"] == 1
        assert next_payload["completion_reason"] == "assistant_confirmation_required"
        assert next_payload["slot_memory"] == {"payee_name": "小红", "amount": "300"}
        assert next_payload["output"]["message"] == "已向小红转账 300 CNY，转账成功"
        assert len(agent_client.tasks) == 1

    asyncio.run(run())


def test_v2_router_message_assistant_protocol_returns_ok_false_when_recognizer_is_unavailable() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_RecognizerFailureRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_recognizer_unavailable_demo"
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "我要跟家里缴3000电费",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["status"] == "failed"
        assert body["completion_state"] == 2
        assert body["completion_reason"] == "router_error"
        assert body["errorCode"] == "ROUTER_LLM_UNAVAILABLE"
        assert body["message"] == "意图识别服务暂不可用，请稍后重试。"
        assert body["slot_memory"] == {}
        assert body["output"] == {}
        assert body["details"]["error_type"] == "ConnectError"

    asyncio.run(run())


def test_v2_router_message_assistant_protocol_supports_multi_intent_graph() -> None:
    async def run() -> None:
        app, _ = _test_v2_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_multi_intent_demo"
            response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "先查余额，再给张三转账 200 元，卡号 6222020100049999999，尾号 1234",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["current_task"] in {item["name"] for item in body["task_list"]}
        assert len(body["task_list"]) == 2
        assert body["intent_code"] in {"query_account_balance", "transfer_money"}
        assert body["completion_state"] in {0, 2}
        assert isinstance(body["message"], str)
        assert body.get("errorCode") is None
        assert body.get("stage") is None
        assert body.get("details") is None

    asyncio.run(run())


def test_v1_message_stream_aligns_recognition_with_graph_card_task_order() -> None:
    async def run() -> None:
        app, orchestrator = _test_v2_app(
            recognizer=_MisorderedRecognitionRecognizer(),
            planner=_GraphCardOrderPlanner(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_stream_graph_order_demo"
            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "先给小明转账500元，再缴燃气费88元",
                    "stream": True,
                    "executionMode": "router_only",
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        message_payloads = _message_payloads(stream_text)
        recognition_payload = message_payloads[0]
        assert recognition_payload["completion_reason"] == "intent_recognized"
        assert recognition_payload["stage"] == "intent_recognition"
        assert recognition_payload["intent_code"] == "transfer_money"
        assert [item["intent_code"] for item in recognition_payload["details"]["primary"]] == [
            "transfer_money",
            "pay_gas_bill",
        ]

        state_payloads = _non_recognition_payloads(message_payloads)
        assert state_payloads
        first_graph_payload = state_payloads[0]
        assert first_graph_payload["intent_code"] == "transfer_money"
        session = orchestrator.session_store.get(session_id)
        assert session.current_graph is not None
        graph_task_order = [
            node.task_id
            for node in sorted(session.current_graph.nodes, key=lambda item: item.position)
        ]
        assert [item["name"] for item in first_graph_payload["task_list"]] == graph_task_order
        assert first_graph_payload["task_list"][0]["status"] == "waiting"

    asyncio.run(run())


def test_v1_message_stream_assistant_protocol_waiting_then_completed() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_stream_waiting_completed_demo"

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小明转账",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            ) as response:
                waiting_text = "".join([chunk async for chunk in response.aiter_text()])

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "200",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            ) as response:
                completed_text = "".join([chunk async for chunk in response.aiter_text()])

        waiting_frames = _parse_sse_frames(waiting_text)
        assert waiting_frames[-1] == ("done", "[DONE]")
        waiting_message_payloads = _message_payloads(waiting_text)
        recognition_payload = waiting_message_payloads[0]
        assert recognition_payload["status"] == "running"
        assert recognition_payload["completion_reason"] == "intent_recognized"
        assert recognition_payload["intent_code"] == "AG_TRANS"
        assert recognition_payload["message"] == "意图识别完成: AG_TRANS"
        assert recognition_payload["stage"] == "intent_recognition"
        assert recognition_payload["details"]["primary"][0]["intent_code"] == "AG_TRANS"
        assert recognition_payload["output"] == {}
        waiting_payload = _non_recognition_payloads(waiting_message_payloads)[0]
        assert waiting_payload["status"] == "waiting_user_input"
        assert waiting_payload["completion_state"] == 0
        assert waiting_payload["current_task"].startswith("task_")
        assert waiting_payload["task_list"] == [{"name": waiting_payload["current_task"], "status": "waiting"}]
        assert waiting_payload["message"] == "请提供金额"
        assert waiting_payload["output"] == {}

        completed_frames = _parse_sse_frames(completed_text)
        assert completed_frames[-1] == ("done", "[DONE]")
        completed_payload = _non_recognition_payloads(_message_payloads(completed_text))[0]
        assert completed_payload["status"] == "waiting_assistant_completion"
        assert completed_payload["completion_state"] == 1
        assert completed_payload["completion_reason"] == "assistant_confirmation_required"
        assert completed_payload["agent_id"] == "AG_TRANS"
        assert completed_payload["message"] == "执行图等待助手确认完成态"
        assert set(completed_payload) == _ASSISTANT_PROTOCOL_OUTPUT_KEYS_WITH_AGENT
        assert completed_payload["task_list"] == [{"name": completed_payload["current_task"], "status": "waiting"}]
        assert completed_payload["output"]["message"] == "已向小明转账 200 CNY，转账成功"
        assert completed_payload["output"]["data"][0]["answer"] == "||200|小明|"

    asyncio.run(run())


def test_v1_message_assistant_protocol_keeps_latest_payee_across_multiple_waiting_turns() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolTransferAgentClient()
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_MultiTurnOverrideTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_multiturn_latest_payee_demo"

            first_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "我要转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert first_turn.status_code == 200
            first_body = first_turn.json()
            assert first_body["status"] == "waiting_user_input"
            assert first_body["slot_memory"] == {}
            assert first_body["message"] == "请提供金额、收款人姓名"

            second_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "小刚",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert second_turn.status_code == 200
            second_body = second_turn.json()
            assert second_body["status"] == "waiting_user_input"
            assert second_body["slot_memory"] == {"payee_name": "小刚"}
            assert second_body["message"] == "请提供金额"

            third_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "小红吧",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert third_turn.status_code == 200
            third_body = third_turn.json()
            assert third_body["status"] == "waiting_user_input"
            assert third_body["slot_memory"] == {"payee_name": "小红"}
            assert third_body["message"] == "请提供金额"

            fourth_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "200",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                        {"name": "currentDisplay", "value": "transfer_confirm_page"},
                        {"name": "agentSessionID", "value": session_id},
                    ],
                },
            )
            assert fourth_turn.status_code == 200
            fourth_body = fourth_turn.json()
            assert fourth_body["status"] == "waiting_assistant_completion"
            assert fourth_body["completion_state"] == 1
            assert fourth_body["slot_memory"] == {"payee_name": "小红", "amount": "200"}
            assert fourth_body["output"]["data"] == [
                {
                    "isSubAgent": "True",
                    "typIntent": "mbpTransfer",
                    "answer": "||200|小红|",
                }
            ]

        assert len(agent_client.tasks) == 1
        assert agent_client.tasks[0].slot_memory == {"payee_name": "小红", "amount": "200"}

    asyncio.run(run())


def test_v1_message_stream_assistant_protocol_emits_graph_level_pending_payload() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(graph_builder=_SingleNodeConfirmGraphBuilder())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": "assistant_stream_graph_pending_demo",
                    "txt": "小刚",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_stream_graph_pending_demo"},
                    ],
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

            json_response = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "assistant_json_graph_pending_demo",
                    "txt": "小刚",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_json_graph_pending_demo"},
                    ],
                },
            )

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        message_payloads = _message_payloads(stream_text)
        recognition_payload = message_payloads[0]
        assert recognition_payload["completion_reason"] == "intent_recognized"
        assert recognition_payload["stage"] == "intent_recognition"
        assert recognition_payload["output"] == {}
        state_payloads = _non_recognition_payloads(message_payloads)
        assert len(state_payloads) == 1
        assert json_response.status_code == 200
        stream_payload = dict(state_payloads[0])
        json_payload = json_response.json()
        assert stream_payload["current_task"].startswith("task_")
        assert json_payload["current_task"].startswith("task_")
        assert stream_payload["task_list"] == [{"name": stream_payload["current_task"], "status": "waiting"}]
        assert json_payload["task_list"] == [{"name": json_payload["current_task"], "status": "waiting"}]
        stream_payload["current_task"] = "task_DYNAMIC"
        json_payload["current_task"] = "task_DYNAMIC"
        stream_payload["task_list"] = [{"name": "task_DYNAMIC", "status": "waiting"}]
        json_payload["task_list"] = [{"name": "task_DYNAMIC", "status": "waiting"}]
        assert stream_payload == json_payload
        assert set(state_payloads[0]) == _ASSISTANT_PROTOCOL_OUTPUT_KEYS
        assert state_payloads[0]["status"] == "draft"
        assert state_payloads[0]["completion_state"] == 0
        assert state_payloads[0]["completion_reason"] == "running"
        assert state_payloads[0]["message"] == "执行图等待节点确认"
        assert state_payloads[0]["output"] == {}

    asyncio.run(run())


def test_v1_message_stream_assistant_protocol_preserves_agent_workflow_frames() -> None:
    async def run() -> None:
        agent_client = _AssistantProtocolWorkflowAgentClient()
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_stream_workflow_demo"
            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "给小明转账200",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        message_payloads = _message_payloads(stream_text)
        recognition_payload = message_payloads[0]
        assert recognition_payload["completion_reason"] == "intent_recognized"
        assert recognition_payload["stage"] == "intent_recognition"
        assert recognition_payload["details"]["primary"][0]["intent_code"] == "AG_TRANS"
        assert recognition_payload["output"] == {}
        state_payloads = _non_recognition_payloads(message_payloads)
        assert [payload["output"]["node_id"] for payload in state_payloads] == [
            "validate_payee",
            "execute_transfer",
        ]
        assert [payload["output"]["message"] for payload in state_payloads] == [
            "收款人校验通过",
            "已向小明转账 200 CNY，转账成功",
        ]
        assert all(set(payload) == _ASSISTANT_PROTOCOL_OUTPUT_KEYS_WITH_AGENT for payload in state_payloads)
        assert [payload["agent_id"] for payload in state_payloads] == ["AG_TRANS", "AG_TRANS"]
        assert state_payloads[0]["status"] == "running"
        assert state_payloads[0]["completion_state"] == 0
        assert state_payloads[0]["message"] == ""
        assert state_payloads[0]["task_list"] == [
            {"name": state_payloads[0]["current_task"], "status": "running"}
        ]
        assert state_payloads[1]["status"] == "waiting_assistant_completion"
        assert state_payloads[1]["completion_state"] == 1
        assert state_payloads[1]["completion_reason"] == "assistant_confirmation_required"
        assert state_payloads[1]["message"] == "执行图等待助手确认完成态"
        assert state_payloads[1]["task_list"] == [
            {"name": state_payloads[1]["current_task"], "status": "waiting"}
        ]
        assert state_payloads[1]["output"]["data"][0]["answer"] == "||200|小明|"

    asyncio.run(run())


def test_v1_message_stream_assistant_protocol_flattens_agent_output_wrapper_without_expanding_contract() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncByteStream(
                    [
                        (
                            'event: message\ndata: {"event":"message","output":{"node_id":"validate_payee",'
                            '"message":"收款人校验通过","completion_state":0,"ishandover":false,'
                            '"data":[{"answer":"收款人校验通过"}],'
                            '"slot_memory":{"payee_name":"小明","amount":"200"}}}\n\n'
                        ).encode("utf-8"),
                        (
                            'event: message\ndata: {"event":"message","output":{"node_id":"execute_transfer",'
                            '"message":"已向小明转账 200 CNY，转账成功","completion_state":2,'
                            '"completion_reason":"agent_final_done","ishandover":true,'
                            '"handOverReason":"已提供收款人和金额交易对象",'
                            '"data":[{"isSubAgent":"True","typIntent":"mbpTransfer","answer":"||200|小明|"}],'
                            '"slot_memory":{"payee_name":"小明","amount":"200"}}}\n\n'
                        ).encode("utf-8"),
                        b"event: done\ndata: [DONE]\n\n",
                    ]
                ),
            )

        intent = _assistant_protocol_ag_trans_intent().model_copy(deep=True)
        intent.agent_url = "https://agent.example.com/transfer"
        raw_agent_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        agent_client = StreamingAgentClient(http_client=raw_agent_http)
        app, _ = _test_v2_app(
            recognizer=_TransferOnlyRecognizer(),
            intents=[intent],
            understanding_validator=_ContractTransferUnderstandingValidator(),
            agent_client=agent_client,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            first_turn = await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "assistant_wrapped_output_demo",
                    "txt": "给小明转账",
                    "stream": False,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_wrapped_output_demo"},
                    ],
                },
            )
            assert first_turn.status_code == 200

            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": "assistant_wrapped_output_demo",
                    "txt": "200",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": "assistant_wrapped_output_demo"},
                    ],
                },
            ) as response:
                stream_text = "".join([chunk async for chunk in response.aiter_text()])

        await raw_agent_http.aclose()

        frames = _parse_sse_frames(stream_text)
        assert frames[-1] == ("done", "[DONE]")
        message_payloads = _message_payloads(stream_text)
        state_payloads = _non_recognition_payloads(message_payloads)
        assert len(state_payloads) == 2
        assert all(set(payload) == _ASSISTANT_PROTOCOL_OUTPUT_KEYS_WITH_AGENT for payload in state_payloads)
        assert [payload["agent_id"] for payload in state_payloads] == ["AG_TRANS", "AG_TRANS"]

        first_payload = state_payloads[0]
        assert first_payload["status"] == "running"
        assert first_payload["completion_state"] == 0
        assert first_payload["message"] == ""
        assert first_payload["slot_memory"] == {"payee_name": "小明", "amount": "200"}
        assert first_payload["output"]["node_id"] == "validate_payee"
        assert first_payload["output"]["message"] == "收款人校验通过"
        assert first_payload["output"]["data"] == [{"answer": "收款人校验通过"}]
        assert "slot_memory" not in first_payload["output"]

        second_payload = state_payloads[1]
        assert second_payload["status"] == "waiting_assistant_completion"
        assert second_payload["completion_state"] == 1
        assert second_payload["completion_reason"] == "assistant_confirmation_required"
        assert second_payload["message"] == "执行图等待助手确认完成态"
        assert second_payload["output"]["node_id"] == "execute_transfer"
        assert second_payload["output"]["message"] == "已向小明转账 200 CNY，转账成功"
        assert second_payload["output"]["data"] == [
            {
                "isSubAgent": "True",
                "typIntent": "mbpTransfer",
                "answer": "||200|小明|",
            }
        ]
        assert second_payload["slot_memory"] == {"payee_name": "小明", "amount": "200"}
        assert "slot_memory" not in second_payload["output"]

    asyncio.run(run())


def test_v1_message_stream_assistant_protocol_surfaces_llm_unavailable() -> None:
    async def run() -> None:
        app, _ = _test_v2_app(
            recognizer=_RecognizerFailureRecognizer(),
            intents=[_assistant_protocol_ag_trans_intent()],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            session_id = "assistant_stream_llm_unavailable_demo"
            async with client.stream(
                "POST",
                "/api/v1/message",
                json={
                    "sessionId": session_id,
                    "txt": "我要跟家里缴3000电费",
                    "stream": True,
                    "config_variables": [
                        {"name": "custID", "value": "C0001"},
                        {"name": "sessionID", "value": session_id},
                    ],
                },
            ) as response:
                raw_text = "".join([chunk async for chunk in response.aiter_text()])

        frames = _parse_sse_frames(raw_text)
        assert frames[-1] == ("done", "[DONE]")
        payload = json.loads(frames[0][1])
        assert frames[0][0] == "message"
        assert payload["status"] == "failed"
        assert payload["errorCode"] == "ROUTER_LLM_UNAVAILABLE"
        assert payload["completion_state"] == 2
        assert payload["message"] == "意图识别服务暂不可用，请稍后重试。"
        assert payload["output"] == {}

    asyncio.run(run())
