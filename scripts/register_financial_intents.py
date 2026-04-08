#!/usr/bin/env python3
"""Register or update the additional financial intents used by the V2 runtime."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


CREDIT_CARD_AGENT_URL = "http://intent-credit-card-agent.intent.svc.cluster.local:8000/api/agent/run"
GAS_BILL_AGENT_URL = "http://intent-gas-bill-agent.intent.svc.cluster.local:8000/api/agent/run"
FOREX_AGENT_URL = "http://intent-forex-agent.intent.svc.cluster.local:8000/api/agent/run"


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8").strip()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def _base_payload(
    *,
    intent_code: str,
    name: str,
    description: str,
    examples: list[str],
    agent_url: str,
    request_schema: dict[str, Any],
    field_mapping: dict[str, str],
    slot_schema: list[dict[str, Any]],
    graph_build_hints: dict[str, Any],
) -> dict[str, Any]:
    return {
        "intent_code": intent_code,
        "name": name,
        "description": description,
        "examples": examples,
        "agent_url": agent_url,
        "status": "active",
        "dispatch_priority": 90,
        "request_schema": request_schema,
        "field_mapping": field_mapping,
        "slot_schema": slot_schema,
        "graph_build_hints": graph_build_hints,
        "resume_policy": "resume_same_task",
    }


def build_payloads() -> list[dict[str, Any]]:
    recent_context_mapping = {
        "conversation.recentMessages": "$context.recent_messages",
        "conversation.longTermMemory": "$context.long_term_memory",
    }
    common_intent_mapping = {
        "intent.code": "$intent.code",
        "intent.name": "$intent.name",
        "intent.description": "$intent.description",
        "intent.examples": "$intent.examples",
    }
    return [
        _base_payload(
            intent_code="query_credit_card_repayment",
            name="查询信用卡还款信息",
            description=(
                "查询信用卡当前账单应还金额、最低还款额和到期日。"
                "用户也可能说信用卡还款信息、信用卡账单、应还多少钱、最低还款、还款日。"
            ),
            examples=[
                "查一下我的信用卡还款信息",
                "帮我看看本期信用卡要还多少钱",
                "查下信用卡账单和最低还款",
                "我的信用卡这个月应还多少",
            ],
            agent_url=CREDIT_CARD_AGENT_URL,
            request_schema={
                "type": "object",
                "required": ["sessionId", "taskId", "input"],
            },
            field_mapping={
                "sessionId": "$session.id",
                "taskId": "$task.id",
                "input": "$message.current",
                "creditCard.cardNumber": "$slot_memory.card_number",
                "creditCard.phoneLast4": "$slot_memory.phone_last_four",
                **recent_context_mapping,
                **common_intent_mapping,
            },
            slot_schema=[
                {
                    "slot_key": "card_number",
                    "label": "信用卡卡号",
                    "description": "需要查询的信用卡卡号",
                    "value_type": "account_number",
                    "required": True,
                    "allow_from_history": True,
                },
                {
                    "slot_key": "phone_last_four",
                    "label": "手机号后4位",
                    "description": "信用卡绑定手机号后4位",
                    "value_type": "phone_last4",
                    "required": True,
                    "allow_from_history": True,
                },
            ],
            graph_build_hints={
                "intent_scope_rule": "单次信用卡账单或还款信息查询是一个 intent。",
                "planner_notes": "卡号和手机号后4位都是槽位，不要拆成节点。用户问应还金额、最低还款、到期日时，仍然是同一个 intent。",
                "single_node_examples": [
                    "查一下我的信用卡还款信息",
                    "帮我看看本期信用卡要还多少钱",
                    "查下信用卡账单和最低还款",
                ],
                "confirm_policy": "auto",
            },
        ),
        _base_payload(
            intent_code="pay_gas_bill",
            name="缴纳天然气费",
            description=(
                "为指定燃气户号缴纳天然气费。"
                "用户也可能说燃气费、煤气费、燃气缴费、天然气缴费。"
            ),
            examples=[
                "帮我交一下天然气费",
                "给燃气户号 88001234 交 88 元",
                "帮我缴一下燃气费",
                "给煤气费充值 100 元",
            ],
            agent_url=GAS_BILL_AGENT_URL,
            request_schema={
                "type": "object",
                "required": ["sessionId", "taskId", "input"],
            },
            field_mapping={
                "sessionId": "$session.id",
                "taskId": "$task.id",
                "input": "$message.current",
                "gas.accountNumber": "$slot_memory.gas_account_number",
                "payment.amount": "$slot_memory.amount",
                **recent_context_mapping,
                **common_intent_mapping,
            },
            slot_schema=[
                {
                    "slot_key": "gas_account_number",
                    "label": "燃气户号",
                    "description": "天然气缴费户号",
                    "value_type": "account_number",
                    "required": True,
                    "allow_from_history": False,
                },
                {
                    "slot_key": "amount",
                    "label": "缴费金额",
                    "description": "本次天然气缴费金额",
                    "value_type": "currency",
                    "required": True,
                    "allow_from_history": False,
                },
            ],
            graph_build_hints={
                "intent_scope_rule": "单次天然气费、燃气费或煤气费缴费动作是一个 intent。",
                "planner_notes": "燃气户号和缴费金额都是槽位，不要拆成节点。用户说天然气费、燃气费、煤气费时都映射到同一个 intent。",
                "single_node_examples": [
                    "帮我交一下天然气费",
                    "给燃气户号 88001234 交 88 元",
                    "帮我缴一下燃气费",
                ],
                "confirm_policy": "auto",
            },
        ),
        _base_payload(
            intent_code="exchange_forex",
            name="换外汇",
            description=(
                "执行单次外汇兑换，支持常见币种之间的换汇。"
                "用户也可能说换外汇、购汇、结汇、把人民币换成美元、把美元换成人民币。"
            ),
            examples=[
                "把 1000 人民币换成美元",
                "我想把 500 美元换成人民币",
                "帮我换 200 欧元",
                "我要购汇，把人民币换成美元",
            ],
            agent_url=FOREX_AGENT_URL,
            request_schema={
                "type": "object",
                "required": ["sessionId", "taskId", "input"],
            },
            field_mapping={
                "sessionId": "$session.id",
                "taskId": "$task.id",
                "input": "$message.current",
                "exchange.sourceCurrency": "$slot_memory.source_currency",
                "exchange.targetCurrency": "$slot_memory.target_currency",
                "exchange.amount": "$slot_memory.amount",
                **recent_context_mapping,
                **common_intent_mapping,
            },
            slot_schema=[
                {
                    "slot_key": "source_currency",
                    "label": "卖出币种",
                    "description": "换汇前币种",
                    "value_type": "string",
                    "required": True,
                    "allow_from_history": False,
                },
                {
                    "slot_key": "target_currency",
                    "label": "买入币种",
                    "description": "换汇后币种",
                    "value_type": "string",
                    "required": True,
                    "allow_from_history": False,
                },
                {
                    "slot_key": "amount",
                    "label": "换汇金额",
                    "description": "本次换汇金额",
                    "value_type": "currency",
                    "required": True,
                    "allow_from_history": False,
                },
            ],
            graph_build_hints={
                "intent_scope_rule": "单次换外汇、购汇或结汇动作是一个 intent。",
                "planner_notes": "币种和金额都是槽位，不要拆成节点。像“把人民币换成美元”“把美元换成人民币”都属于同一个 intent。",
                "single_node_examples": [
                    "把 1000 人民币换成美元",
                    "我想把 500 美元换成人民币",
                    "我要购汇，把人民币换成美元",
                ],
                "confirm_policy": "auto",
            },
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register additional financial intents.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--activate", action="store_true", help="Activate intents after upsert.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for payload in build_payloads():
        intent_code = payload["intent_code"]
        base_url = args.base_url.rstrip("/")
        detail_url = f"{base_url}/api/admin/intents/{intent_code}"
        status, body = _http_json("GET", detail_url)
        if status == 404:
            create_url = f"{base_url}/api/admin/intents"
            status, body = _http_json("POST", create_url, payload)
        elif status == 200:
            status, body = _http_json("PUT", detail_url, payload)
        else:
            print(f"[FAIL] inspect {intent_code}: status={status}, body={body}")
            return 1

        if status not in {200, 201}:
            print(f"[FAIL] upsert {intent_code}: status={status}, body={body}")
            return 1
        print(f"[OK] upsert {intent_code}: status={status}")

        if args.activate:
            activate_url = f"{base_url}/api/admin/intents/{intent_code}/activate"
            activate_status, activate_body = _http_json("POST", activate_url)
            if activate_status not in {200, 204}:
                print(f"[FAIL] activate {intent_code}: status={activate_status}, body={activate_body}")
                return 1
            print(f"[OK] activate {intent_code}: status={activate_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
