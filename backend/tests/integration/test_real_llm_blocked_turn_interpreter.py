from __future__ import annotations

import asyncio
import os

import pytest

from router_service.core.graph.planner import LLMGraphTurnInterpreter
from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.graph_domain import ExecutionGraphState, GraphNodeState
from router_service.core.support.llm_client import LangChainLLMClient
from router_service.settings import Settings


def test_real_llm_blocked_turn_interpreter_handles_switch_resume_and_cancel() -> None:
    if os.getenv("RUN_REAL_LLM_TEST") != "1":
        pytest.skip("Set RUN_REAL_LLM_TEST=1 to run the real LLM blocked-turn smoke test.")

    async def run() -> None:
        settings = Settings.from_env()
        if not settings.llm_connection_ready or settings.default_llm_model is None:
            pytest.skip("ROUTER_LLM_API_BASE_URL and ROUTER_LLM_MODEL/ROUTER_LLM_RECOGNIZER_MODEL are required.")

        llm_client = LangChainLLMClient(
            base_url=settings.llm_api_base_url or "",
            api_key=settings.llm_api_key,
            default_model=settings.default_llm_model,
            temperature=settings.llm_temperature,
            timeout_seconds=settings.llm_timeout_seconds,
            rate_limit_max_retries=settings.llm_rate_limit_max_retries,
            rate_limit_retry_delay_seconds=settings.llm_rate_limit_retry_delay_seconds,
            extra_headers=settings.llm_headers,
            structured_output_method=settings.llm_structured_output_method,
        )
        interpreter = LLMGraphTurnInterpreter(llm_client, model=settings.default_llm_model)
        intents = [
            IntentDefinition(
                intent_code="query_account_balance",
                name="查询账户余额",
                description="查询银行卡账户余额，需要卡号和手机号后4位。",
                examples=["帮我查一下余额", "查余额", "查询银行卡余额"],
                agent_url="https://agent.example.com/query_account_balance",
                primary_threshold=0.7,
                candidate_threshold=0.4,
            ),
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账，需要收款人姓名、收款卡号、手机号后4位和金额。",
                examples=["给张三转 200 元", "帮我转账给李四"],
                agent_url="https://agent.example.com/transfer_money",
                primary_threshold=0.7,
                candidate_threshold=0.4,
            ),
            IntentDefinition(
                intent_code="pay_gas_bill",
                name="缴纳天然气费",
                description="缴纳天然气费，需要燃气户号和缴费金额。",
                examples=["帮我交一下天然气费", "给燃气户号 88001234 交 88 元"],
                agent_url="https://agent.example.com/pay_gas_bill",
                primary_threshold=0.7,
                candidate_threshold=0.4,
            ),
        ]
        waiting_node = GraphNodeState(
            intent_code="transfer_money",
            title="转账",
            confidence=0.9,
            position=0,
            source_fragment="帮我给小明转账",
        )
        current_graph = ExecutionGraphState(source_message="帮我给小明转账")
        cases = [
            ("先不转了，帮我查余额", "replan", "query_account_balance"),
            ("先别转账了，我要交燃气费", "replan", "pay_gas_bill"),
            ("给张三转200", "resume_current", None),
            ("算了，不转了", "cancel_current", None),
        ]

        try:
            for message, expected_action, expected_intent in cases:
                decision = await interpreter.interpret_blocked_turn(
                    mode="waiting_node",
                    message=message,
                    waiting_node=waiting_node,
                    current_graph=current_graph,
                    active_intents=intents,
                    recent_messages=["user: 帮我给小明转账", "assistant: 请提供转账金额和收款卡号"],
                    long_term_memory=[],
                    recommend_task=None,
                )

                assert decision.action == expected_action
                if expected_intent is None:
                    assert decision.target_intent_code is None
                    assert decision.primary_intents == []
                else:
                    assert decision.target_intent_code == expected_intent
                    assert decision.primary_intents
                    assert decision.primary_intents[0].intent_code == expected_intent
        finally:
            await llm_client.aclose()

    asyncio.run(run())
