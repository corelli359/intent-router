from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any
from uuid import uuid4

import orjson
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse, StreamingResponse
from pydantic import BaseModel, Field


JSONResponse = ORJSONResponse

_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
CARD_NUMBER_RE = re.compile(r"(?<!\d)(\d{6,20})(?!\d)")
PHONE_LAST4_RE = re.compile(r"(?:后4位|后四位|尾号)\D*(\d{4})")
RECIPIENT_PHONE_LAST4_RE = re.compile(
    r"(?:收款人手机号后4位|收款人手机号后四位|收款手机号后4位|收款手机号后四位|收款人尾号|收款尾号)\D*(\d{4})"
)
AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|人民币)?")
CHANGE_AMOUNT_RE = re.compile(r"(?:改成|改为|改到|金额改成|金额改为|金额改到)\D*(\d+(?:\.\d+)?)")
ACTION_AMOUNT_RE = re.compile(
    r"(?:转账|转给|转|汇款|付款|支付|缴费|交|缴|还款|换汇|换|买入|卖出)[^\d]{0,8}(\d+(?:\.\d+)?)"
)
TRANSFER_ACTION_RE = re.compile(
    r"(?:给|向)(?P<recipient>[\u4e00-\u9fffA-Za-z]{1,16})(?:转账|转|汇款)[^\d]{0,8}(?P<amount>\d+(?:\.\d+)?)"
)
TRANSFER_ACTION_RE_ALT = re.compile(
    r"(?:给|向)(?P<recipient>[\u4e00-\u9fffA-Za-z]{1,16})[^\d]{0,8}(?P<amount>\d+(?:\.\d+)?)\s*(?:元|块|人民币)?(?:转账|转|汇款)?"
)
NAME_RE = re.compile(
    r"(?:给|向|转给|转账给)([\u4e00-\u9fffA-Za-z]{1,16}?)(?=(?:转账|转|汇款|付款|支付|卡号|银行卡|手机号|尾号|后4位|后四位|金额|[，,。\s]|$))"
)
GAS_ACCOUNT_RE = re.compile(r"(?:燃气户号|户号)\D*(\d{6,20})")
ACCOUNT_HINT_RE = re.compile(r"(?:卡号|银行卡号|账户|账号|收款卡号|收款账户|收款账号)\D*(\d{6,20})")
BALANCE_THRESHOLD_RE = re.compile(r"(?:余额|大于|超过|高于)[^\d]{0,8}(\d+(?:\.\d+)?)")
TRANSFER_INTENT_CODES = {"transfer_money", "AG_TRANS"}


class ChatCompletionMessage(BaseModel):
    role: str
    content: Any


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatCompletionMessage]
    stream: bool = False
    temperature: float | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None


def _json_dumps(value: Any) -> str:
    return orjson.dumps(value).decode("utf-8")


def _json_loads(raw_text: str) -> Any:
    return orjson.loads(raw_text)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if isinstance(item, dict):
                if item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
                    continue
                if "text" in item:
                    chunks.append(str(item["text"]))
                    continue
            chunks.append(str(item))
        return "".join(chunks)
    return str(content or "")


def _normalize_text(value: str) -> str:
    return _NORMALIZE_RE.sub("", value).lower()


def _find_section(text: str, label: str, end_labels: Iterable[str]) -> str:
    marker = f"{label}\n"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end_positions = [text.find(candidate, start) for candidate in end_labels]
    valid_positions = [position for position in end_positions if position >= 0]
    end = min(valid_positions) if valid_positions else len(text)
    return text[start:end].strip()


def _parse_json_section(text: str, label: str, end_labels: Iterable[str]) -> Any:
    section = _find_section(text, label, end_labels)
    if not section:
        return None
    return _json_loads(section)


def _known_intent_score(intent_code: str, message: str) -> float:
    lowered = message.lower()
    if intent_code in TRANSFER_INTENT_CODES:
        if "转账" in message or "汇款" in message or re.search(r"(?:给|向).{0,12}(?:转|汇)", message):
            return 0.96
    if intent_code == "query_account_balance":
        if "查余额" in message or "查询余额" in message or ("余额" in message and "信用卡" not in message):
            return 0.95
    if intent_code == "query_credit_card_repayment":
        if "信用卡" in message and ("还款" in message or "账单" in message):
            return 0.95
    if intent_code == "pay_gas_bill":
        if ("燃气" in message or "天然气" in message) and ("缴" in message or "交" in message):
            return 0.95
    if intent_code == "exchange_forex":
        if "换汇" in message or "购汇" in message or "外汇" in message or ("美元" in message and "人民币" in message):
            return 0.95
    if intent_code.startswith("domain://"):
        return 0.0
    if "不" in lowered and any(token in lowered for token in ("不要", "不用", "取消")):
        return 0.0
    return 0.0


def _generic_intent_score(intent_payload: dict[str, Any], message: str) -> tuple[float, str]:
    intent_code = str(intent_payload.get("intent_code", "")).strip()
    known_score = _known_intent_score(intent_code, message)
    if known_score > 0:
        return known_score, f"matched known intent {intent_code}"

    normalized_message = _normalize_text(message)
    best_score = 0.0
    best_reason = "no_match"
    for candidate in (
        intent_payload.get("name"),
        intent_payload.get("description"),
        intent_payload.get("domain_name"),
        intent_payload.get("domain_description"),
        *(intent_payload.get("keywords") or []),
        *(intent_payload.get("examples") or []),
        *(intent_payload.get("routing_examples") or []),
    ):
        candidate_text = str(candidate or "").strip()
        normalized_candidate = _normalize_text(candidate_text)
        if not normalized_candidate:
            continue
        if normalized_candidate in normalized_message or normalized_message in normalized_candidate:
            score = 0.9 if len(normalized_candidate) > 2 else 0.75
            if score > best_score:
                best_score = score
                best_reason = f"matched catalog text {candidate_text!r}"
        elif candidate_text and candidate_text in message:
            if 0.82 > best_score:
                best_score = 0.82
                best_reason = f"matched catalog text {candidate_text!r}"
    return best_score, best_reason


def _recognize_matches(message: str, intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for intent in intents:
        score, reason = _generic_intent_score(intent, message)
        primary_threshold = float(intent.get("primary_threshold", 0.7) or 0.7)
        candidate_threshold = float(intent.get("candidate_threshold", 0.5) or 0.5)
        threshold = min(primary_threshold, candidate_threshold)
        if score < threshold:
            continue
        matches.append(
            {
                "intent_code": intent["intent_code"],
                "confidence": round(min(0.99, score), 2),
                "reason": reason,
                "dispatch_priority": int(intent.get("dispatch_priority", 0) or 0),
            }
        )
    matches.sort(key=lambda item: (item["dispatch_priority"], item["confidence"]), reverse=True)
    for item in matches:
        item.pop("dispatch_priority", None)
    return matches


def _build_slot(slot_key: str, value: Any, source_text: str, confidence: float = 0.96) -> dict[str, Any]:
    return {
        "slot_key": slot_key,
        "value": value,
        "source": "user_message",
        "source_text": source_text,
        "confidence": confidence,
    }


def _extract_card_number(text: str) -> tuple[str | None, str | None]:
    match = ACCOUNT_HINT_RE.search(text) or CARD_NUMBER_RE.search(text)
    if match is None:
        return None, None
    return match.group(1), match.group(0)


def _extract_phone_last4(text: str) -> tuple[str | None, str | None]:
    match = PHONE_LAST4_RE.search(text)
    if match is None:
        return None, None
    return match.group(1), match.group(0)


def _extract_recipient_phone_last4(text: str) -> tuple[str | None, str | None]:
    match = RECIPIENT_PHONE_LAST4_RE.search(text)
    if match is None:
        return None, None
    return match.group(1), match.group(0)


def _extract_amount(text: str) -> tuple[str | None, str | None]:
    for pattern in (CHANGE_AMOUNT_RE, ACTION_AMOUNT_RE, AMOUNT_RE):
        match = pattern.search(text)
        if match is not None:
            return match.group(1), match.group(0)
    return None, None


def _extract_transfer_actions(text: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for pattern in (TRANSFER_ACTION_RE, TRANSFER_ACTION_RE_ALT):
        for match in pattern.finditer(text):
            action = {
                "recipient_name": match.group("recipient"),
                "amount": match.group("amount"),
                "source_fragment": match.group(0),
            }
            if action not in actions:
                actions.append(action)
    return actions


def _extract_slot_payload(intent_payload: dict[str, Any], message: str, existing_slot_memory: dict[str, Any]) -> dict[str, Any]:
    intent_code = str(intent_payload.get("intent_code", "")).strip()
    slots: list[dict[str, Any]] = []
    slot_keys = {str(slot.get("slot_key", "")) for slot in (intent_payload.get("slot_schema") or [])}

    if intent_code in {"query_account_balance", "query_credit_card_repayment"}:
        card_number, card_source = _extract_card_number(message)
        phone_last4, phone_source = _extract_phone_last4(message)
        if card_number and "card_number" in slot_keys and "card_number" not in existing_slot_memory:
            slots.append(_build_slot("card_number", card_number, card_source or message))
        if phone_last4 and "phone_last_four" in slot_keys and "phone_last_four" not in existing_slot_memory:
            slots.append(_build_slot("phone_last_four", phone_last4, phone_source or message))
    elif intent_code == "pay_gas_bill":
        gas_match = GAS_ACCOUNT_RE.search(message)
        amount, amount_source = _extract_amount(message)
        if gas_match is not None and "gas_account_number" in slot_keys and "gas_account_number" not in existing_slot_memory:
            slots.append(_build_slot("gas_account_number", gas_match.group(1), gas_match.group(0)))
        if amount and "amount" in slot_keys and "amount" not in existing_slot_memory:
            slots.append(_build_slot("amount", amount, amount_source or message))
    elif intent_code in TRANSFER_INTENT_CODES:
        recipient_match = NAME_RE.search(message)
        amount, amount_source = _extract_amount(message)
        recipient_card, recipient_card_source = _extract_card_number(message)
        recipient_phone, recipient_phone_source = _extract_recipient_phone_last4(message)
        recipient_name_key = "recipient_name" if "recipient_name" in slot_keys else "payee_name" if "payee_name" in slot_keys else None
        recipient_card_key = (
            "recipient_card_number"
            if "recipient_card_number" in slot_keys
            else "payee_card_no"
            if "payee_card_no" in slot_keys
            else None
        )
        recipient_phone_key = (
            "recipient_phone_last_four"
            if "recipient_phone_last_four" in slot_keys
            else "payee_phone"
            if "payee_phone" in slot_keys
            else None
        )
        if recipient_match is not None and recipient_name_key and recipient_name_key not in existing_slot_memory:
            slots.append(_build_slot(recipient_name_key, recipient_match.group(1), recipient_match.group(0)))
        if amount and "amount" in slot_keys and "amount" not in existing_slot_memory:
            slots.append(_build_slot("amount", amount, amount_source or message))
        if recipient_card and recipient_card_key and recipient_card_key not in existing_slot_memory:
            slots.append(_build_slot(recipient_card_key, recipient_card, recipient_card_source or message))
        if recipient_phone and recipient_phone_key and recipient_phone_key not in existing_slot_memory:
            slots.append(_build_slot(recipient_phone_key, recipient_phone, recipient_phone_source or message))
    elif intent_code == "exchange_forex":
        amount, amount_source = _extract_amount(message)
        if "amount" in slot_keys and amount and "amount" not in existing_slot_memory:
            slots.append(_build_slot("amount", amount, amount_source or message))
        upper_text = message.upper()
        source_currency = None
        target_currency = None
        if "人民币" in message or "CNY" in upper_text:
            source_currency = "CNY"
        if "美元" in message or "USD" in upper_text:
            target_currency = "USD"
        if source_currency and "source_currency" in slot_keys and "source_currency" not in existing_slot_memory:
            slots.append(_build_slot("source_currency", source_currency, message))
        if target_currency and "target_currency" in slot_keys and "target_currency" not in existing_slot_memory:
            slots.append(_build_slot("target_currency", target_currency, message))

    return {"slots": slots, "ambiguousSlotKeys": []}


def _default_title(intent_payload: dict[str, Any], slot_memory: dict[str, Any]) -> str:
    intent_code = str(intent_payload.get("intent_code", ""))
    recipient_name = slot_memory.get("recipient_name") or slot_memory.get("payee_name")
    if intent_code in TRANSFER_INTENT_CODES and recipient_name and slot_memory.get("amount"):
        return f"给{recipient_name}转账 {slot_memory['amount']} 元"
    if intent_code == "query_account_balance":
        return "查询账户余额"
    if intent_code == "query_credit_card_repayment":
        return "查询信用卡还款信息"
    if intent_code == "pay_gas_bill":
        return "缴纳天然气费"
    if intent_code == "exchange_forex":
        return "换汇"
    return str(intent_payload.get("name") or intent_code)


def _build_node(intent_payload: dict[str, Any], *, confidence: float, source_fragment: str, slot_memory: dict[str, Any]) -> dict[str, Any]:
    slot_bindings = [
        _build_slot(slot_key, value, source_fragment, confidence)
        for slot_key, value in slot_memory.items()
    ]
    return {
        "intent_code": intent_payload["intent_code"],
        "title": _default_title(intent_payload, slot_memory),
        "confidence": round(min(0.99, confidence), 2),
        "source_fragment": source_fragment,
        "slot_memory": slot_memory,
        "slot_bindings": slot_bindings,
    }


def _intent_payload_by_code(intents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(intent.get("intent_code", "")): intent for intent in intents if intent.get("intent_code")}


def _build_planner_payload(message: str, matched_intents: list[dict[str, Any]]) -> dict[str, Any]:
    intents_by_code = _intent_payload_by_code(
        [
            {
                **(item.get("definition") or {}),
                "intent_code": item.get("intent_code"),
            }
            for item in matched_intents
            if item.get("definition")
        ]
    )
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    matched_codes = [str(item.get("intent_code", "")) for item in matched_intents if item.get("intent_code")]

    if "query_account_balance" in matched_codes and "transfer_money" in matched_codes:
        balance_intent = intents_by_code.get("query_account_balance", {"intent_code": "query_account_balance", "name": "查询账户余额"})
        transfer_intent = intents_by_code.get("transfer_money", {"intent_code": "transfer_money", "name": "转账"})
        balance_memory = _extract_slot_payload(balance_intent, message, {}).get("slots", [])
        balance_slot_memory = {item["slot_key"]: item["value"] for item in balance_memory}
        balance_node = _build_node(
            balance_intent,
            confidence=0.97,
            source_fragment="查余额" if "查余额" in message else message,
            slot_memory=balance_slot_memory,
        )
        nodes.append(balance_node)
        transfer_actions = _extract_transfer_actions(message) or [
            {
                "recipient_name": _extract_slot_payload(transfer_intent, message, {}).get("slots", [{}])[0].get("value")
                if _extract_slot_payload(transfer_intent, message, {}).get("slots")
                else None,
                "amount": (_extract_amount(message)[0] or ""),
                "source_fragment": message,
            }
        ]
        threshold_matches = [match.group(1) for match in BALANCE_THRESHOLD_RE.finditer(message)]
        for index, action in enumerate(transfer_actions):
            slot_memory = {
                key: value
                for key, value in {
                    "recipient_name": action.get("recipient_name"),
                    "amount": action.get("amount"),
                }.items()
                if value
            }
            node = _build_node(
                transfer_intent,
                confidence=0.94 - (index * 0.01),
                source_fragment=action.get("source_fragment") or message,
                slot_memory=slot_memory,
            )
            nodes.append(node)
            threshold = threshold_matches[index] if index < len(threshold_matches) else threshold_matches[0] if threshold_matches else "0"
            edges.append(
                {
                    "source_index": 0,
                    "target_index": len(nodes) - 1,
                    "relation_type": "conditional" if threshold_matches else "sequential",
                    "label": f"余额大于 {threshold} 时执行" if threshold_matches else "按顺序执行",
                    "condition": {
                        "expected_statuses": ["completed"],
                        "left_key": "balance",
                        "operator": ">",
                        "right_value": float(threshold) if "." in threshold else int(threshold),
                    }
                    if threshold_matches
                    else None,
                }
            )
        return {
            "summary": "识别到多个事项，已生成执行图",
            "needs_confirmation": len(nodes) > 1,
            "nodes": nodes,
            "edges": edges,
        }

    if not matched_codes:
        return {"summary": "未识别到明确事项", "needs_confirmation": False, "nodes": [], "edges": []}

    for item in matched_intents:
        intent_code = str(item.get("intent_code", ""))
        intent_payload = intents_by_code.get(intent_code, item.get("definition") or {"intent_code": intent_code, "name": intent_code})
        slot_payload = _extract_slot_payload(intent_payload, message, {})
        slot_memory = {slot["slot_key"]: slot["value"] for slot in slot_payload["slots"]}
        nodes.append(
            _build_node(
                intent_payload,
                confidence=float(item.get("confidence", 0.95) or 0.95),
                source_fragment=message,
                slot_memory=slot_memory,
            )
        )

    for index in range(1, len(nodes)):
        edges.append(
            {
                "source_index": index - 1,
                "target_index": index,
                "relation_type": "sequential",
                "label": "按识别顺序执行",
                "condition": None,
            }
        )

    return {
        "summary": "识别到多个事项，已生成执行图" if len(nodes) > 1 else f"识别到事项：{nodes[0]['title']}",
        "needs_confirmation": len(nodes) > 1,
        "nodes": nodes,
        "edges": edges,
    }


def _handle_recognizer(system_text: str, human_text: str) -> dict[str, Any]:
    del system_text
    message = _find_section(
        human_text,
        "当前消息:",
        ["最近对话(JSON):\n", "长期记忆(JSON):\n", "可选 domain 列表(JSON):\n", "当前 domain 里的 leaf intents(JSON):\n", "已注册意图清单(JSON):\n"],
    )
    intents = (
        _parse_json_section(human_text, "已注册意图清单(JSON):", ["请输出 JSON:\n"])
        or _parse_json_section(human_text, "可选 domain 列表(JSON):", ["请输出 JSON:\n"])
        or _parse_json_section(human_text, "当前 domain 里的 leaf intents(JSON):", ["请输出 JSON:\n"])
        or []
    )
    return {"matches": _recognize_matches(message, intents)}


def _handle_slot_extractor(system_text: str, human_text: str) -> dict[str, Any]:
    del system_text
    message = _find_section(
        human_text,
        "当前消息:",
        ["当前节点原始片段:\n", "意图定义(JSON):\n", "已有槽位(JSON):\n", "请输出 JSON:\n"],
    )
    intent_payload = _parse_json_section(human_text, "意图定义(JSON):", ["已有槽位(JSON):\n", "请输出 JSON:\n"]) or {}
    existing_slot_memory = _parse_json_section(human_text, "已有槽位(JSON):", ["请输出 JSON:\n"]) or {}
    return _extract_slot_payload(intent_payload, message, existing_slot_memory)


def _handle_graph_planner(system_text: str, human_text: str) -> dict[str, Any]:
    del system_text
    message = _find_section(
        human_text,
        "当前用户消息:",
        ["最近对话(JSON):\n", "长期记忆(JSON):\n", "本轮已识别 intent(JSON):\n", "请输出 JSON:\n"],
    )
    matched_intents = _parse_json_section(human_text, "本轮已识别 intent(JSON):", ["请输出 JSON:\n"]) or []
    return _build_planner_payload(message, matched_intents)


def _handle_unified_graph_builder(system_text: str, human_text: str) -> dict[str, Any]:
    del system_text
    message = _find_section(
        human_text,
        "当前用户消息:",
        ["最近对话(JSON):\n", "长期记忆(JSON):\n", "已有识别提示(JSON，可为空):\n", "已注册意图清单(JSON):\n", "请输出 JSON:\n"],
    )
    intents = _parse_json_section(human_text, "已注册意图清单(JSON):", ["请输出 JSON:\n"]) or []
    recognition_hint = _parse_json_section(human_text, "已有识别提示(JSON，可为空):", ["已注册意图清单(JSON):\n", "请输出 JSON:\n"]) or {}
    primary_hints = recognition_hint.get("primary") or []
    candidate_hints = recognition_hint.get("candidates") or []
    matches = primary_hints or _recognize_matches(message, intents)
    matched_intents = []
    intents_by_code = _intent_payload_by_code(intents)
    for match in matches:
        intent_payload = intents_by_code.get(str(match.get("intent_code", "")))
        if intent_payload is None:
            continue
        matched_intents.append(
            {
                "intent_code": match["intent_code"],
                "confidence": match.get("confidence", 0.95),
                "reason": match.get("reason", "fake-llm matched"),
                "definition": intent_payload,
            }
        )
    plan = _build_planner_payload(message, matched_intents)
    return {
        "summary": plan["summary"],
        "needs_confirmation": plan["needs_confirmation"],
        "primary_intents": [
            {
                "intent_code": item["intent_code"],
                "confidence": item["confidence"],
                "reason": item.get("reason", "fake-llm matched"),
            }
            for item in matched_intents
        ],
        "candidate_intents": list(candidate_hints),
        "nodes": plan["nodes"],
        "edges": plan["edges"],
    }


def _handle_turn_interpreter(system_text: str, human_text: str) -> dict[str, Any]:
    del system_text
    mode = _find_section(
        human_text,
        "模式:",
        ["当前用户消息:\n", "当前等待节点(JSON):\n", "当前执行图(JSON):\n", "待确认执行图(JSON):\n", "请输出 JSON:\n"],
    )
    message = _find_section(
        human_text,
        "当前用户消息:",
        ["当前等待节点(JSON):\n", "当前执行图(JSON):\n", "待确认执行图(JSON):\n", "本轮识别主意图(JSON):\n", "请输出 JSON:\n"],
    )
    waiting_node = _parse_json_section(human_text, "当前等待节点(JSON):", ["当前执行图(JSON):\n", "待确认执行图(JSON):\n", "本轮识别主意图(JSON):\n", "请输出 JSON:\n"]) or {}
    primary_intents = _parse_json_section(human_text, "本轮识别主意图(JSON):", ["本轮识别候选意图(JSON):\n", "请输出 JSON:\n"]) or []

    if any(token in message for token in ("取消", "算了", "不要")):
        return {
            "action": "cancel_pending_graph" if mode == "pending_graph" else "cancel_current",
            "reason": "用户明确表示取消当前流程",
            "target_intent_code": None,
        }
    if mode == "pending_graph" and any(token in message for token in ("确认", "开始", "执行")):
        return {"action": "confirm_pending_graph", "reason": "用户确认执行图", "target_intent_code": None}
    if primary_intents:
        target_intent_code = str(primary_intents[0].get("intent_code", "")).strip() or None
        waiting_intent_code = str(waiting_node.get("intent_code", "")).strip() or None
        if target_intent_code and target_intent_code != waiting_intent_code:
            return {
                "action": "replan",
                "reason": f"用户切换到新的意图 {target_intent_code}",
                "target_intent_code": target_intent_code,
            }
    return {
        "action": "resume_current" if mode == "waiting_node" else "wait",
        "reason": "继续当前流程",
        "target_intent_code": None,
    }


def _handle_proactive_recommendation(system_text: str, human_text: str) -> dict[str, Any]:
    del system_text
    items = _parse_json_section(human_text, "推荐事项清单(JSON):", ["用户回复:\n", "请输出 JSON:\n"]) or []
    message = _find_section(human_text, "用户回复:", ["请输出 JSON:\n"])
    selected_ids: list[str] = []
    selected_intents: list[str] = []
    lowered = message.lower()
    if "都不要" in message or "不用" in message:
        return {
            "route_mode": "no_selection",
            "selectedRecommendationIds": [],
            "selectedIntents": [],
            "hasUserModification": False,
            "modificationReasons": [],
            "reason": "用户明确拒绝所有推荐项",
        }
    if "都要" in message:
        selected_ids = [str(item.get("recommendationItemId", "")) for item in items]
        selected_intents = [str(item.get("intentCode", "")) for item in items]
    elif "第一个" in message and items:
        selected_ids = [str(items[0].get("recommendationItemId", ""))]
        selected_intents = [str(items[0].get("intentCode", ""))]
    elif "第二个" in message and len(items) >= 2:
        selected_ids = [str(items[1].get("recommendationItemId", ""))]
        selected_intents = [str(items[1].get("intentCode", ""))]
    if not selected_ids:
        return {
            "route_mode": "switch_to_free_dialog",
            "selectedRecommendationIds": [],
            "selectedIntents": [],
            "hasUserModification": False,
            "modificationReasons": [],
            "reason": "用户没有按推荐项选择，切回自由对话",
        }
    has_modification = any(token in lowered for token in ("改", "如果", "先", "再", "同时", "顺便"))
    route_mode = "interactive_graph" if has_modification else "direct_execute"
    return {
        "route_mode": route_mode,
        "selectedRecommendationIds": selected_ids,
        "selectedIntents": selected_intents,
        "hasUserModification": has_modification,
        "modificationReasons": ["用户修改了推荐项执行方式"] if has_modification else [],
        "reason": "根据用户选择进入推荐执行流程",
    }


def _generate_content(request: ChatCompletionRequest) -> str:
    system_text = "\n".join(_message_text(message.content) for message in request.messages if message.role == "system")
    human_text = "\n".join(
        _message_text(message.content)
        for message in request.messages
        if message.role in {"human", "user"}
    )
    if "多意图识别与执行图构建器" in system_text:
        payload = _handle_unified_graph_builder(system_text, human_text)
    elif "多意图执行图规划器" in system_text:
        payload = _handle_graph_planner(system_text, human_text)
    elif "槽位抽取器" in system_text:
        payload = _handle_slot_extractor(system_text, human_text)
    elif "回合解释器" in system_text:
        payload = _handle_turn_interpreter(system_text, human_text)
    elif "主动推荐场景下的意图分流器" in system_text:
        payload = _handle_proactive_recommendation(system_text, human_text)
    else:
        payload = _handle_recognizer(system_text, human_text)
    return _json_dumps(payload)


def _chat_completion_response(*, model: str, content: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": max(1, len(content) // 4),
            "total_tokens": max(1, len(content) // 4),
        },
    }


async def _stream_chat_completion(*, model: str, content: str) -> AsyncIterator[bytes]:
    chunk_id = f"chatcmpl-{uuid4().hex}"
    created = int(time.time())
    first_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {_json_dumps(first_chunk)}\n\n".encode("utf-8")
    last_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }
        ],
    }
    yield f"data: {_json_dumps(last_chunk)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


app = FastAPI(title="Fake LLM Service", version="0.1.0", default_response_class=ORJSONResponse)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    content = _generate_content(request)
    if request.stream:
        return StreamingResponse(
            _stream_chat_completion(model=request.model, content=content),
            media_type="text/event-stream",
        )
    return JSONResponse(_chat_completion_response(model=request.model, content=content))
