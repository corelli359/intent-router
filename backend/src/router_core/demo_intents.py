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
        intent_code="query_order_status",
        name="查询订单状态",
        description="查询订单状态、物流状态、订单进度。",
        examples=["帮我查下订单状态", "订单 123 到哪了", "查一下订单 456"],
        keywords=["订单", "物流", "状态", "发货"],
        agent_url=_agent_url("QUERY_ORDER_STATUS_AGENT_URL", "intent-order-agent", local_port=8101),
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
            "order.orderId": "$slot_memory.order_id",
        },
    ),
    IntentDefinition(
        intent_code="cancel_appointment",
        name="取消预约",
        description="取消明天或指定日期的预约、面试、上门服务。",
        examples=["帮我取消明天的预约", "取消一下上门预约"],
        keywords=["取消", "预约", "明天", "上门"],
        agent_url=_agent_url("CANCEL_APPOINTMENT_AGENT_URL", "intent-appointment-agent", local_port=8102),
        dispatch_priority=80,
        primary_threshold=0.62,
        candidate_threshold=0.42,
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
            "appointment.dateText": "$slot_memory.appointment_date",
            "appointment.bookingReference": "$slot_memory.booking_reference",
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
        intent_code="transfer_money",
        name="转账",
        description="执行转账、收款账户确认、付款账户确认。",
        examples=["给张三转 200 元", "帮我转账给李四"],
        keywords=["转账", "给", "元", "账户", "付款"],
        agent_url="mock://transfer_money",
        dispatch_priority=95,
        primary_threshold=0.72,
        candidate_threshold=0.5,
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
