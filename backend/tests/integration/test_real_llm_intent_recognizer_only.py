from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from config.settings import Settings  # noqa: E402
from router_core.domain import IntentDefinition  # noqa: E402
from router_core.llm_client import LangChainLLMClient  # noqa: E402
from router_core.recognizer import LLMIntentRecognizer, NullIntentRecognizer  # noqa: E402


def test_real_llm_intent_recognizer_only() -> None:
    """Real LLM smoke test for pure intent recognition.

    This test intentionally exercises only:
    LangChainLLMClient -> LLMIntentRecognizer -> recognize()

    It does not start the router runtime, does not use orchestrator,
    does not build an execution graph, and does not fall back to
    keyword / regex recognition.
    """

    if os.getenv("RUN_REAL_LLM_TEST") != "1":
        pytest.skip("Set RUN_REAL_LLM_TEST=1 to run the real LLM intent recognizer smoke test.")

    async def run() -> None:
        settings = Settings.from_env()
        if not settings.llm_connection_ready or settings.default_llm_model is None:
            pytest.skip("ROUTER_LLM_API_BASE_URL and ROUTER_LLM_MODEL/ROUTER_LLM_RECOGNIZER_MODEL are required.")

        llm_client = LangChainLLMClient(
            base_url=settings.llm_api_base_url or "",
            api_key=settings.llm_api_key,
            default_model=settings.default_llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            extra_headers=settings.llm_headers,
            structured_output_method=settings.llm_structured_output_method,
        )
        recognizer = LLMIntentRecognizer(
            llm_client,
            model=settings.llm_recognizer_model or settings.llm_model,
            fallback=NullIntentRecognizer(),
        )

        intents = [
            IntentDefinition(
                intent_code="query_account_balance",
                name="查询账户余额",
                description="查询账户余额，需要卡号和手机号后4位。",
                examples=["帮我查一下余额", "查余额", "查询银行卡余额"],
                agent_url="https://agent.example.com/query_account_balance",
                primary_threshold=0.7,
                candidate_threshold=0.4,
            ),
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="执行转账，需要收款人姓名、收款卡号、收款人手机号后4位和金额。",
                examples=["给张三转 200 元", "帮我转账给李四"],
                agent_url="https://agent.example.com/transfer_money",
                primary_threshold=0.7,
                candidate_threshold=0.4,
            ),
            IntentDefinition(
                intent_code="pay_bill",
                name="生活缴费",
                description="缴纳电费、水费、燃气费、话费等。",
                examples=["帮我交电费", "给我缴一下水费"],
                agent_url="https://agent.example.com/pay_bill",
                primary_threshold=0.7,
                candidate_threshold=0.4,
            ),
        ]

        result = await recognizer.recognize(
            message="先帮我查余额，如果够的话再给张三转 200 元",
            intents=intents,
            recent_messages=[],
            long_term_memory=[],
        )

        print("primary:")
        for item in result.primary:
            print(f"- {item.intent_code} confidence={item.confidence} reason={item.reason}")

        print("candidates:")
        for item in result.candidates:
            print(f"- {item.intent_code} confidence={item.confidence} reason={item.reason}")

        assert result.primary or result.candidates

    asyncio.run(run())
