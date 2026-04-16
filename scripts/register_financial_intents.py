#!/usr/bin/env python3
"""Register or update the builtin finance intents used by the V2 router runtime."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


ACCOUNT_BALANCE_AGENT_URL = "http://intent-order-agent.intent.svc.cluster.local:8000/api/agent/run"
TRANSFER_MONEY_AGENT_URL = "http://intent-appointment-agent.intent.svc.cluster.local:8000/api/agent/run"
CREDIT_CARD_AGENT_URL = "http://intent-credit-card-agent.intent.svc.cluster.local:8000/api/agent/run"
GAS_BILL_AGENT_URL = "http://intent-gas-bill-agent.intent.svc.cluster.local:8000/api/agent/run"
FOREX_AGENT_URL = "http://intent-forex-agent.intent.svc.cluster.local:8000/api/agent/run"


COMMON_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["sessionId", "taskId", "input"],
}

COMMON_CONTEXT_MAPPING = {
    "sessionId": "$session.id",
    "taskId": "$task.id",
    "input": "$message.current",
    "conversation.recentMessages": "$context.recent_messages",
    "conversation.longTermMemory": "$context.long_term_memory",
}


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    host_header: str | None = None,
) -> tuple[int, Any]:
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if host_header:
        headers["Host"] = host_header

    request = urllib.request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8").strip()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def _field(
    *,
    field_code: str,
    label: str,
    semantic_definition: str,
    value_type: str,
    aliases: list[str] | None = None,
    examples: list[str] | None = None,
    counter_examples: list[str] | None = None,
    format_hint: str = "",
    normalization_hint: str = "",
    validation_hint: str = "",
) -> dict[str, Any]:
    return {
        "field_code": field_code,
        "label": label,
        "semantic_definition": semantic_definition,
        "value_type": value_type,
        "aliases": aliases or [],
        "examples": examples or [],
        "counter_examples": counter_examples or [],
        "format_hint": format_hint,
        "normalization_hint": normalization_hint,
        "validation_hint": validation_hint,
    }


def _slot(
    *,
    slot_key: str,
    field_code: str,
    role: str,
    label: str,
    description: str,
    semantic_definition: str,
    value_type: str,
    required: bool,
    allow_from_history: bool,
    aliases: list[str] | None = None,
    examples: list[str] | None = None,
    counter_examples: list[str] | None = None,
    prompt_hint: str = "",
) -> dict[str, Any]:
    return {
        "slot_key": slot_key,
        "field_code": field_code,
        "role": role,
        "label": label,
        "description": description,
        "semantic_definition": semantic_definition,
        "value_type": value_type,
        "required": required,
        "allow_from_history": allow_from_history,
        "aliases": aliases or [],
        "examples": examples or [],
        "counter_examples": counter_examples or [],
        "prompt_hint": prompt_hint,
    }


def _payload(
    *,
    intent_code: str,
    name: str,
    description: str,
    domain_code: str,
    domain_name: str,
    domain_description: str,
    examples: list[str],
    routing_examples: list[str],
    agent_url: str,
    dispatch_priority: int,
    field_mapping: dict[str, str],
    field_catalog: list[dict[str, Any]],
    slot_schema: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "intent_code": intent_code,
        "name": name,
        "description": description,
        "domain_code": domain_code,
        "domain_name": domain_name,
        "domain_description": domain_description,
        "examples": examples,
        "routing_examples": routing_examples,
        "agent_url": agent_url,
        "is_leaf_intent": True,
        "parent_intent_code": "",
        "status": "active",
        "dispatch_priority": dispatch_priority,
        "request_schema": COMMON_REQUEST_SCHEMA,
        "field_mapping": field_mapping,
        "field_catalog": field_catalog,
        "slot_schema": slot_schema,
        "resume_policy": "resume_same_task",
    }


def build_payloads() -> list[dict[str, Any]]:
    return [
        _payload(
            intent_code="query_account_balance",
            name="查询账户余额",
            description=(
                "查询指定银行卡当前余额。"
                "需要用户明确提供银行卡号和绑定手机号后4位，用于身份核验。"
            ),
            domain_code="account_service",
            domain_name="账户服务",
            domain_description="账户余额、账户状态、账户基础查询相关意图。",
            examples=[
                "查一下我的账户余额",
                "帮我查银行卡还有多少钱",
                "看下这张卡余额",
                "查询余额",
            ],
            routing_examples=[
                "查一下余额",
                "看看我银行卡还有多少钱",
                "查询账户余额",
            ],
            agent_url=ACCOUNT_BALANCE_AGENT_URL,
            dispatch_priority=100,
            field_mapping={
                **COMMON_CONTEXT_MAPPING,
                "account.cardNumber": "$slot_memory.card_number",
                "account.phoneLast4": "$slot_memory.phone_last_four",
            },
            field_catalog=[
                _field(
                    field_code="account_card_number",
                    label="银行卡号",
                    semantic_definition="当前要查询余额的银行卡卡号。",
                    value_type="account_number",
                    aliases=["卡号", "银行卡号", "账户卡号"],
                    examples=["6222020100043219999"],
                    counter_examples=["手机号后4位", "交易金额"],
                    format_hint="仅保留卡号数字，不保留空格与分隔符。",
                    normalization_hint="去掉空格、横线等非数字字符。",
                    validation_hint="通常为 12 到 19 位数字。",
                ),
                _field(
                    field_code="bound_phone_last_four",
                    label="手机号后4位",
                    semantic_definition="该银行卡绑定手机号的后4位，用于身份核验。",
                    value_type="phone_last4",
                    aliases=["手机号后4位", "手机尾号", "后四位"],
                    examples=["6666"],
                    counter_examples=["完整手机号", "银行卡号"],
                    format_hint="只保留最后 4 位数字。",
                    normalization_hint="若用户给出完整手机号，仅保留最后 4 位。",
                    validation_hint="必须是 4 位数字。",
                ),
            ],
            slot_schema=[
                _slot(
                    slot_key="card_number",
                    field_code="account_card_number",
                    role="account_identity",
                    label="银行卡号",
                    description="需要查询余额的银行卡卡号。",
                    semantic_definition="当前余额查询任务对应的目标银行卡号。",
                    value_type="account_number",
                    required=True,
                    allow_from_history=True,
                    aliases=["卡号", "银行卡号", "账户卡号"],
                    examples=["6222020100043219999"],
                    counter_examples=["6666", "88元"],
                    prompt_hint="优先识别当前任务明确提到的卡号，允许从同一用户历史确认信息中复用。",
                ),
                _slot(
                    slot_key="phone_last_four",
                    field_code="bound_phone_last_four",
                    role="customer_verification",
                    label="手机号后4位",
                    description="银行卡绑定手机号后4位。",
                    semantic_definition="用于余额查询身份核验的手机号后4位。",
                    value_type="phone_last4",
                    required=True,
                    allow_from_history=True,
                    aliases=["手机号后4位", "手机尾号", "后四位"],
                    examples=["6666"],
                    counter_examples=["6222020100043219999", "100"],
                    prompt_hint="如果用户给出完整手机号，只保留最后 4 位。",
                ),
            ],
        ),
        _payload(
            intent_code="transfer_money",
            name="转账",
            description=(
                "向指定收款人发起转账。"
                "按业务确认槽位执行：amount、payee_name 为必填；"
                "payer_card_no、payer_card_remark、payee_card_no、payee_card_remark、payee_card_bank、payee_phone 为可选。"
            ),
            domain_code="transfer",
            domain_name="转账",
            domain_description="行内外转账、付款、汇款相关意图。",
            examples=[
                "给张三尾号8899那张卡转500",
                "帮我给王芳转账，收款卡号 6217000012345678901",
                "向李四汇款 2000 元到尾号 5566 的卡",
                "给 Emma 的收款卡转账",
            ],
            routing_examples=[
                "给张三转账",
                "转 500 给王芳的卡",
                "我要汇款",
            ],
            agent_url=TRANSFER_MONEY_AGENT_URL,
            dispatch_priority=95,
            field_mapping={
                **COMMON_CONTEXT_MAPPING,
                "transfer.amount": "$slot_memory.amount",
                "payer.cardNo": "$slot_memory.payer_card_no",
                "payer.cardRemark": "$slot_memory.payer_card_remark",
                "payee.name": "$slot_memory.payee_name",
                "payee.cardNo": "$slot_memory.payee_card_no",
                "payee.cardRemark": "$slot_memory.payee_card_remark",
                "payee.cardBank": "$slot_memory.payee_card_bank",
                "payee.phone": "$slot_memory.payee_phone",
            },
            field_catalog=[
                _field(
                    field_code="amount",
                    label="金额",
                    semantic_definition="本次转账实际金额。",
                    value_type="currency",
                    aliases=["金额", "转账金额", "汇款金额"],
                    examples=["500", "2000.50"],
                    counter_examples=["银行卡号", "币种"],
                    format_hint="输出数字字符串，不保留货币单位。",
                    normalization_hint="去掉元、块、人民币等单位。",
                    validation_hint="必须是正数金额。",
                ),
                _field(
                    field_code="payer_card_no",
                    label="付款卡卡号/尾号",
                    semantic_definition="本次转账使用的付款卡卡号或卡尾号。",
                    value_type="string",
                    aliases=["付款卡", "付款卡号", "付款卡尾号"],
                    examples=["6222020100043219999", "8888"],
                    counter_examples=["收款人姓名", "转账金额"],
                ),
                _field(
                    field_code="payer_card_remark",
                    label="付款卡备注",
                    semantic_definition="本次转账付款卡对应的备注名称。",
                    value_type="string",
                    aliases=["付款卡备注", "付款账户备注"],
                    examples=["工资卡", "日常卡"],
                ),
                _field(
                    field_code="payee_name",
                    label="收款人姓名",
                    semantic_definition="本次转账对应的收款人姓名。",
                    value_type="string",
                    aliases=["收款人姓名", "收款人", "对方姓名"],
                    examples=["小红", "张三", "弟弟"],
                    counter_examples=["6222020100043219999", "500"],
                ),
                _field(
                    field_code="payee_card_no",
                    label="收款卡卡号/尾号",
                    semantic_definition="本次转账对应的收款卡卡号或卡尾号。",
                    value_type="string",
                    aliases=["收款卡号", "收款卡尾号", "收款卡卡号/尾号", "对方卡号", "对方卡尾号"],
                    examples=["6217000012345678901", "5566"],
                    counter_examples=["李四", "工资卡", "转账金额"],
                ),
                _field(
                    field_code="payee_card_remark",
                    label="收款卡备注",
                    semantic_definition="收款卡的备注名称。",
                    value_type="string",
                    aliases=["收款卡备注", "收款账户备注"],
                    examples=["房租卡", "家用卡"],
                ),
                _field(
                    field_code="payee_card_bank",
                    label="收款卡银行",
                    semantic_definition="收款卡所属银行名称。",
                    value_type="string",
                    aliases=["收款银行", "收款卡银行"],
                    examples=["招商银行", "中国银行"],
                ),
                _field(
                    field_code="payee_phone",
                    label="收款手机号/尾号",
                    semantic_definition="收款人手机号或手机号尾号。",
                    value_type="string",
                    aliases=["收款手机号", "收款手机号尾号", "收款手机号/尾号"],
                    examples=["13800138000", "1234"],
                ),
            ],
            slot_schema=[
                _slot(
                    slot_key="amount",
                    field_code="amount",
                    role="transaction_amount",
                    label="金额",
                    description="本次转账金额。",
                    semantic_definition="转账动作需要执行的金额。",
                    value_type="currency",
                    required=True,
                    allow_from_history=False,
                    aliases=["金额", "转账金额", "汇款金额"],
                    examples=["500", "2000.50"],
                    counter_examples=["1234", "6222020100043219999"],
                    prompt_hint="若当前消息明确包含转账金额，应优先提取到 amount。",
                ),
                _slot(
                    slot_key="payer_card_no",
                    field_code="payer_card_no",
                    role="payer_account",
                    label="付款卡卡号/尾号",
                    description="付款卡卡号或尾号。",
                    semantic_definition="当前转账使用的付款卡标识。",
                    value_type="string",
                    required=False,
                    allow_from_history=False,
                    aliases=["付款卡", "付款卡号", "付款卡尾号"],
                    examples=["6222020100043219999", "8888"],
                    counter_examples=["小红", "500"],
                    prompt_hint="只有用户明确指向付款卡时才提取，不要把收款卡信息误填到付款卡。",
                ),
                _slot(
                    slot_key="payer_card_remark",
                    field_code="payer_card_remark",
                    role="payer_account_remark",
                    label="付款卡备注",
                    description="付款卡备注。",
                    semantic_definition="当前转账付款卡的备注名称。",
                    value_type="string",
                    required=False,
                    allow_from_history=False,
                    aliases=["付款卡备注", "付款账户备注"],
                    examples=["工资卡", "日常卡"],
                    counter_examples=["6222020100043219999", "小红"],
                    prompt_hint="只有用户明确说出付款卡备注时才填充该槽位。",
                ),
                _slot(
                    slot_key="payee_name",
                    field_code="payee_name",
                    role="payee_name",
                    label="收款人姓名",
                    description="收款人姓名。",
                    semantic_definition="当前转账的收款人姓名。",
                    value_type="string",
                    required=True,
                    allow_from_history=False,
                    aliases=["收款人姓名", "收款人", "对方姓名"],
                    examples=["小红", "张三", "李四"],
                    counter_examples=["1234", "人民币"],
                    prompt_hint="只有用户明确给出收款人姓名时才提取，不要把卡号填到 payee_name。",
                ),
                _slot(
                    slot_key="payee_card_no",
                    field_code="payee_card_no",
                    role="payee_account",
                    label="收款卡卡号/尾号",
                    description="收款卡卡号或尾号。",
                    semantic_definition="当前转账的收款卡标识。",
                    value_type="string",
                    required=False,
                    allow_from_history=False,
                    aliases=["收款卡号", "收款卡尾号", "收款卡卡号/尾号", "对方卡号", "对方卡尾号"],
                    examples=["6222020100043219999", "1234"],
                    counter_examples=["张三", "人民币"],
                    prompt_hint="只有用户明确给出收款卡号或尾号时才提取，不要把收款人姓名填到 payee_card_no。",
                ),
                _slot(
                    slot_key="payee_card_remark",
                    field_code="payee_card_remark",
                    role="payee_account_remark",
                    label="收款卡备注",
                    description="收款卡备注。",
                    semantic_definition="当前收款卡的备注名称。",
                    value_type="string",
                    required=False,
                    allow_from_history=False,
                    aliases=["收款卡备注", "收款账户备注"],
                    examples=["房租卡", "家用卡"],
                    counter_examples=["小红", "500"],
                    prompt_hint="只有用户明确提供收款卡备注时才填充。",
                ),
                _slot(
                    slot_key="payee_card_bank",
                    field_code="payee_card_bank",
                    role="payee_bank",
                    label="收款卡银行",
                    description="收款卡所属银行。",
                    semantic_definition="当前收款卡对应的银行名称。",
                    value_type="string",
                    required=False,
                    allow_from_history=False,
                    aliases=["收款银行", "收款卡银行"],
                    examples=["招商银行", "中国银行"],
                    counter_examples=["500", "小红"],
                    prompt_hint="只有用户明确指出收款银行时才填充。",
                ),
                _slot(
                    slot_key="payee_phone",
                    field_code="payee_phone",
                    role="payee_phone",
                    label="收款手机号/尾号",
                    description="收款手机号或手机号尾号。",
                    semantic_definition="当前收款对象对应的手机号或尾号。",
                    value_type="string",
                    required=False,
                    allow_from_history=False,
                    aliases=["收款手机号", "收款手机号尾号", "收款手机号/尾号"],
                    examples=["13800138000", "1234"],
                    counter_examples=["500", "招商银行"],
                    prompt_hint="只有用户明确给出收款手机号或尾号时才填充。",
                ),
            ],
        ),
        _payload(
            intent_code="query_credit_card_repayment",
            name="查询信用卡还款信息",
            description=(
                "查询信用卡当期应还金额、最低还款额和到期日。"
                "需要用户提供信用卡卡号和绑定手机号后4位。"
            ),
            domain_code="credit_card",
            domain_name="信用卡",
            domain_description="信用卡账单、还款、额度相关意图。",
            examples=[
                "查一下我的信用卡还款信息",
                "这期信用卡要还多少钱",
                "帮我看一下信用卡账单",
                "查询最低还款",
            ],
            routing_examples=[
                "查信用卡还款",
                "信用卡账单还有多少",
                "最低还款是多少",
            ],
            agent_url=CREDIT_CARD_AGENT_URL,
            dispatch_priority=92,
            field_mapping={
                **COMMON_CONTEXT_MAPPING,
                "creditCard.cardNumber": "$slot_memory.card_number",
                "creditCard.phoneLast4": "$slot_memory.phone_last_four",
            },
            field_catalog=[
                _field(
                    field_code="credit_card_number",
                    label="信用卡卡号",
                    semantic_definition="当前要查询账单的信用卡卡号。",
                    value_type="account_number",
                    aliases=["信用卡卡号", "卡号", "信用卡号码"],
                    examples=["6225888800001234567"],
                    counter_examples=["手机号后4位", "最低还款额"],
                    format_hint="仅保留卡号数字。",
                    normalization_hint="去掉空格与分隔符。",
                    validation_hint="通常为 12 到 19 位数字。",
                ),
                _field(
                    field_code="credit_card_phone_last_four",
                    label="手机号后4位",
                    semantic_definition="该信用卡绑定手机号的后4位。",
                    value_type="phone_last4",
                    aliases=["手机号后4位", "手机尾号", "后四位"],
                    examples=["6666"],
                    counter_examples=["完整手机号", "卡号"],
                    format_hint="只保留后 4 位数字。",
                    normalization_hint="若用户提供完整手机号，仅保留最后 4 位。",
                    validation_hint="必须是 4 位数字。",
                ),
            ],
            slot_schema=[
                _slot(
                    slot_key="card_number",
                    field_code="credit_card_number",
                    role="card_identity",
                    label="信用卡卡号",
                    description="需要查询账单的信用卡卡号。",
                    semantic_definition="当前信用卡还款信息查询任务的目标卡号。",
                    value_type="account_number",
                    required=True,
                    allow_from_history=True,
                    aliases=["信用卡卡号", "卡号", "信用卡号码"],
                    examples=["6225888800001234567"],
                    counter_examples=["6666", "3200"],
                    prompt_hint="允许从同一用户已确认的信用卡任务历史中复用卡号。",
                ),
                _slot(
                    slot_key="phone_last_four",
                    field_code="credit_card_phone_last_four",
                    role="customer_verification",
                    label="手机号后4位",
                    description="信用卡绑定手机号后4位。",
                    semantic_definition="用于信用卡还款信息查询核验的手机号后4位。",
                    value_type="phone_last4",
                    required=True,
                    allow_from_history=True,
                    aliases=["手机号后4位", "手机尾号", "后四位"],
                    examples=["6666"],
                    counter_examples=["6225888800001234567", "3200"],
                    prompt_hint="如果用户给出完整手机号，只保留最后 4 位。",
                ),
            ],
        ),
        _payload(
            intent_code="pay_gas_bill",
            name="缴纳天然气费",
            description=(
                "为指定燃气户号缴纳天然气费。"
                "必须提供燃气户号和本次缴费金额。"
            ),
            domain_code="payment",
            domain_name="缴费",
            domain_description="水电燃气、话费、生活缴费相关意图。",
            examples=[
                "帮我交一下天然气费",
                "给燃气户号 88001234 交 88 元",
                "煤气费充值 100 元",
                "缴一下燃气费",
            ],
            routing_examples=[
                "交燃气费",
                "燃气缴费",
                "给煤气费充值",
            ],
            agent_url=GAS_BILL_AGENT_URL,
            dispatch_priority=90,
            field_mapping={
                **COMMON_CONTEXT_MAPPING,
                "gas.accountNumber": "$slot_memory.gas_account_number",
                "payment.amount": "$slot_memory.amount",
            },
            field_catalog=[
                _field(
                    field_code="gas_account_number",
                    label="燃气户号",
                    semantic_definition="天然气缴费账号或户号。",
                    value_type="account_number",
                    aliases=["燃气户号", "天然气户号", "煤气户号"],
                    examples=["88001234"],
                    counter_examples=["缴费金额", "订单号"],
                    format_hint="仅保留数字。",
                    normalization_hint="去掉空格与非数字字符。",
                    validation_hint="通常为 6 到 20 位数字。",
                ),
                _field(
                    field_code="gas_payment_amount",
                    label="缴费金额",
                    semantic_definition="本次天然气缴费金额。",
                    value_type="currency",
                    aliases=["金额", "缴费金额", "充值金额"],
                    examples=["88", "100"],
                    counter_examples=["燃气户号", "手机号后4位"],
                    format_hint="输出数字字符串，不保留元、块。",
                    normalization_hint="去掉货币单位。",
                    validation_hint="必须是正数金额。",
                ),
            ],
            slot_schema=[
                _slot(
                    slot_key="gas_account_number",
                    field_code="gas_account_number",
                    role="billing_account",
                    label="燃气户号",
                    description="天然气缴费户号。",
                    semantic_definition="执行天然气缴费所需的目标户号。",
                    value_type="account_number",
                    required=True,
                    allow_from_history=False,
                    aliases=["燃气户号", "天然气户号", "煤气户号"],
                    examples=["88001234"],
                    counter_examples=["88", "100元"],
                    prompt_hint="不能把金额数字误识别为燃气户号。",
                ),
                _slot(
                    slot_key="amount",
                    field_code="gas_payment_amount",
                    role="billing_amount",
                    label="缴费金额",
                    description="本次天然气缴费金额。",
                    semantic_definition="执行天然气缴费动作的金额。",
                    value_type="currency",
                    required=True,
                    allow_from_history=False,
                    aliases=["金额", "缴费金额", "充值金额"],
                    examples=["88", "100"],
                    counter_examples=["88001234", "卡号"],
                    prompt_hint="优先识别带元、块、充值、缴费等语义的金额表达。",
                ),
            ],
        ),
        _payload(
            intent_code="exchange_forex",
            name="换外汇",
            description=(
                "执行一次外汇兑换。"
                "当前执行至少需要卖出币种、买入币种和换汇金额；可选收集扣款卡号与手机号后4位。"
            ),
            domain_code="wealth_management",
            domain_name="理财",
            domain_description="换汇、外汇、购汇、结汇等理财与资产配置相关意图。",
            examples=[
                "把 1000 人民币换成美元",
                "我想把 500 美元换成人民币",
                "帮我换 200 欧元",
                "我要购汇",
            ],
            routing_examples=[
                "换外汇",
                "人民币换美元",
                "我要购汇",
            ],
            agent_url=FOREX_AGENT_URL,
            dispatch_priority=88,
            field_mapping={
                **COMMON_CONTEXT_MAPPING,
                "account.cardNumber": "$slot_memory.card_number",
                "account.phoneLast4": "$slot_memory.phone_last_four",
                "exchange.sourceCurrency": "$slot_memory.source_currency",
                "exchange.targetCurrency": "$slot_memory.target_currency",
                "exchange.amount": "$slot_memory.amount",
            },
            field_catalog=[
                _field(
                    field_code="forex_source_currency",
                    label="卖出币种",
                    semantic_definition="用户打算卖出或扣减的币种。",
                    value_type="string",
                    aliases=["卖出币种", "源币种", "付款币种"],
                    examples=["人民币", "美元", "CNY", "USD"],
                    counter_examples=["汇率", "金额"],
                    normalization_hint="统一映射为 ISO 货币代码，例如 CNY、USD、EUR。",
                ),
                _field(
                    field_code="forex_target_currency",
                    label="买入币种",
                    semantic_definition="用户打算换入的目标币种。",
                    value_type="string",
                    aliases=["买入币种", "目标币种", "兑换后币种"],
                    examples=["美元", "人民币", "USD", "CNY"],
                    counter_examples=["汇率", "金额"],
                    normalization_hint="统一映射为 ISO 货币代码，例如 CNY、USD、EUR。",
                ),
                _field(
                    field_code="forex_amount",
                    label="换汇金额",
                    semantic_definition="本次换汇金额。",
                    value_type="currency",
                    aliases=["金额", "换汇金额", "购汇金额"],
                    examples=["1000", "500"],
                    counter_examples=["银行卡号", "手机号后4位"],
                    format_hint="输出数字字符串，不保留货币单位。",
                    normalization_hint="去掉元、美元等单位，仅保留数值。",
                    validation_hint="必须是正数金额。",
                ),
                _field(
                    field_code="forex_card_number",
                    label="扣款卡号",
                    semantic_definition="本次换汇扣款银行卡号，可选。",
                    value_type="account_number",
                    aliases=["卡号", "扣款卡号", "银行卡号"],
                    examples=["6222020100043219999"],
                    counter_examples=["手机号后4位", "金额"],
                ),
                _field(
                    field_code="forex_phone_last_four",
                    label="手机号后4位",
                    semantic_definition="换汇校验所需手机号后4位，可选。",
                    value_type="phone_last4",
                    aliases=["手机号后4位", "手机尾号", "后四位"],
                    examples=["1234"],
                    counter_examples=["银行卡号", "金额"],
                ),
            ],
            slot_schema=[
                _slot(
                    slot_key="source_currency",
                    field_code="forex_source_currency",
                    role="sell_currency",
                    label="卖出币种",
                    description="换汇前卖出的币种。",
                    semantic_definition="当前换汇任务中被卖出的币种。",
                    value_type="string",
                    required=True,
                    allow_from_history=False,
                    aliases=["卖出币种", "源币种", "付款币种"],
                    examples=["人民币", "美元", "CNY", "USD"],
                    counter_examples=["汇率", "金额"],
                    prompt_hint="统一归一化为 ISO 代码，例如 人民币=>CNY，美元=>USD。",
                ),
                _slot(
                    slot_key="target_currency",
                    field_code="forex_target_currency",
                    role="buy_currency",
                    label="买入币种",
                    description="换汇后买入的币种。",
                    semantic_definition="当前换汇任务中被买入的目标币种。",
                    value_type="string",
                    required=True,
                    allow_from_history=False,
                    aliases=["买入币种", "目标币种", "兑换后币种"],
                    examples=["美元", "人民币", "USD", "CNY"],
                    counter_examples=["汇率", "金额"],
                    prompt_hint="统一归一化为 ISO 代码，例如 人民币=>CNY，美元=>USD。",
                ),
                _slot(
                    slot_key="amount",
                    field_code="forex_amount",
                    role="exchange_amount",
                    label="换汇金额",
                    description="本次换汇金额。",
                    semantic_definition="当前换汇任务需要执行的金额。",
                    value_type="currency",
                    required=True,
                    allow_from_history=False,
                    aliases=["金额", "换汇金额", "购汇金额"],
                    examples=["1000", "500"],
                    counter_examples=["6222020100043219999", "1234"],
                    prompt_hint="如果一句话里同时有币种与数字，应优先把数字识别为换汇金额。",
                ),
                _slot(
                    slot_key="card_number",
                    field_code="forex_card_number",
                    role="debit_account",
                    label="扣款卡号",
                    description="本次换汇使用的扣款卡号，可选。",
                    semantic_definition="换汇时可能需要的扣款银行卡号。",
                    value_type="account_number",
                    required=False,
                    allow_from_history=False,
                    aliases=["卡号", "扣款卡号", "银行卡号"],
                    examples=["6222020100043219999"],
                    counter_examples=["1234", "1000"],
                    prompt_hint="当前版本不因缺少该槽位阻塞执行，但若用户明确提供则应保留。",
                ),
                _slot(
                    slot_key="phone_last_four",
                    field_code="forex_phone_last_four",
                    role="customer_verification",
                    label="手机号后4位",
                    description="换汇校验所需手机号后4位，可选。",
                    semantic_definition="换汇场景下可选的手机号后4位校验信息。",
                    value_type="phone_last4",
                    required=False,
                    allow_from_history=False,
                    aliases=["手机号后4位", "手机尾号", "后四位"],
                    examples=["1234"],
                    counter_examples=["1000", "6222020100043219999"],
                    prompt_hint="当前版本不因缺少该槽位阻塞执行，但若用户明确提供则应保留。",
                ),
            ],
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register builtin finance intents.")
    parser.add_argument("--base-url", default="http://127.0.0.1")
    parser.add_argument("--host-header", default=None, help="Optional Host header, useful for ingress routing.")
    parser.add_argument("--activate", action="store_true", help="Activate intents after upsert.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")

    for payload in build_payloads():
        intent_code = payload["intent_code"]
        detail_url = f"{base_url}/api/admin/intents/{intent_code}"
        status, body = _http_json("GET", detail_url, host_header=args.host_header)
        if status == 404:
            create_url = f"{base_url}/api/admin/intents"
            status, body = _http_json("POST", create_url, payload, host_header=args.host_header)
        elif status == 200:
            status, body = _http_json("PUT", detail_url, payload, host_header=args.host_header)
        else:
            print(f"[FAIL] inspect {intent_code}: status={status}, body={body}")
            return 1

        if status not in {200, 201}:
            print(f"[FAIL] upsert {intent_code}: status={status}, body={body}")
            return 1
        print(f"[OK] upsert {intent_code}: status={status}")

        if args.activate:
            activate_url = f"{base_url}/api/admin/intents/{intent_code}/activate"
            activate_status, activate_body = _http_json(
                "POST",
                activate_url,
                host_header=args.host_header,
            )
            if activate_status not in {200, 204}:
                print(f"[FAIL] activate {intent_code}: status={activate_status}, body={activate_body}")
                return 1
            print(f"[OK] activate {intent_code}: status={activate_status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
