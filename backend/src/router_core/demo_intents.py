from __future__ import annotations

import os

from config.settings import _load_local_env_files
from router_core.domain import IntentDefinition


_load_local_env_files()


def _svc_base_url(service_name: str, *, local_port: int) -> str:
    namespace = os.getenv("INTENT_ROUTER_NAMESPACE", "intent")
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        return f"http://{service_name}.{namespace}.svc.cluster.local:8000"
    return f"http://127.0.0.1:{local_port}"


def _agent_url(env_name: str, service_name: str, *, local_port: int) -> str:
    override = os.getenv(env_name)
    if override:
        return override
    return f"{_svc_base_url(service_name, local_port=local_port)}/api/agent/run"


DEMO_INTENTS: list[IntentDefinition] = [
    IntentDefinition(
        intent_code="query_account_balance",
        name="查询账户余额",
        description="查询用户账户余额。需要收集卡号和手机号后4位，信息齐全后返回余额结果。",
        examples=["帮我查一下账户余额", "查余额", "查询银行卡余额"],
        keywords=["余额", "账户", "银行卡", "查余额"],
        agent_url=_agent_url("QUERY_ACCOUNT_BALANCE_AGENT_URL", "intent-order-agent", local_port=8101),
        dispatch_priority=100,
        primary_threshold=0.68,
        candidate_threshold=0.45,
        request_schema={
            "type": "object",
            "required": ["sessionId", "taskId", "input", "conversation.recentMessages"],
        },
        field_mapping={
            "sessionId": "$session.id",
            "taskId": "$task.id",
            "input": "$message.current",
            "customer.custId": "$session.cust_id",
            "conversation.recentMessages": "$context.recent_messages",
            "conversation.longTermMemory": "$context.long_term_memory",
            "account.cardNumber": "$slot_memory.card_number",
            "account.phoneLast4": "$slot_memory.phone_last_four",
        },
    ),
    IntentDefinition(
        intent_code="transfer_money",
        name="转账",
        description="执行转账。需要收集收款人姓名、收款卡号、收款人手机号后4位和转账金额。",
        examples=["给张三转 200 元", "帮我给李四转账", "转账到对方银行卡"],
        keywords=["转账", "收款人", "卡号", "金额", "汇款"],
        agent_url=_agent_url("TRANSFER_MONEY_AGENT_URL", "intent-appointment-agent", local_port=8102),
        dispatch_priority=95,
        primary_threshold=0.72,
        candidate_threshold=0.5,
        request_schema={
            "type": "object",
            "required": ["sessionId", "taskId", "input", "conversation.recentMessages"],
        },
        field_mapping={
            "sessionId": "$session.id",
            "taskId": "$task.id",
            "input": "$message.current",
            "customer.custId": "$session.cust_id",
            "conversation.recentMessages": "$context.recent_messages",
            "conversation.longTermMemory": "$context.long_term_memory",
            "recipient.name": "$slot_memory.recipient_name",
            "recipient.cardNumber": "$slot_memory.recipient_card_number",
            "recipient.phoneLast4": "$slot_memory.recipient_phone_last_four",
            "transfer.amount": "$slot_memory.amount",
        },
    ),
    IntentDefinition(
        intent_code="update_shipping_address",
        name="修改收货地址",
        description="更新订单收货地址、配送地址。",
        examples=["帮我改一下收货地址", "更新配送地址"],
        keywords=["地址", "收货", "配送", "改成"],
        agent_url="mock://update_shipping_address",
        dispatch_priority=90,
        primary_threshold=0.65,
        candidate_threshold=0.45,
    ),
    IntentDefinition(
        intent_code="pay_bill",
        name="缴费",
        description="水电煤、话费、生活缴费。",
        examples=["帮我缴电费", "交一下话费"],
        keywords=["缴费", "交费", "电费", "水费", "话费"],
        agent_url="mock://pay_bill",
        dispatch_priority=70,
        primary_threshold=0.72,
        candidate_threshold=0.55,
    ),
]
