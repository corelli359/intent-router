from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from config.settings import Settings  # noqa: E402
from router_core.domain import Task  # noqa: E402
from router_core.llm_client import LangChainLLMClient  # noqa: E402


def _masked_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


async def _run() -> None:
    settings = Settings.from_env()
    if not settings.llm_connection_ready or not settings.llm_api_key:
        raise RuntimeError("ROUTER_LLM_* env is incomplete. Please fill .env.local first.")

    client = LangChainLLMClient(
        base_url=settings.llm_api_base_url or "",
        api_key=settings.llm_api_key,
        default_model=settings.default_llm_model or "",
        timeout_seconds=settings.llm_timeout_seconds,
        extra_headers=settings.llm_headers,
        structured_output_method=settings.llm_structured_output_method,
    )

    print(
        json.dumps(
            {
                "config": {
                    "base_url": settings.llm_api_base_url,
                    "model": settings.llm_model,
                    "recognizer_backend": settings.recognizer_backend,
                    "structured_output_method": settings.llm_structured_output_method,
                    "api_key_masked": _masked_key(settings.llm_api_key),
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    recognition = await client.recognize(
        message="帮我给张三转 200 元，顺便查一下订单 123 的状态",
        recent_messages=["user: 你好", "assistant: 你好，请问需要什么帮助"],
        long_term_memory=["常用收款人：张三"],
        intents=[
            {
                "intent_code": "transfer_money",
                "name": "转账",
                "description": "执行转账、付款账户确认、转账结果查询",
                "examples": ["给张三转200元", "帮我转账给李四"],
                "keywords": ["转账", "付款", "收款人"],
                "agent_url": "llm://default",
                "status": "active",
                "dispatch_priority": 100,
                "primary_threshold": 0.7,
                "candidate_threshold": 0.5,
            },
            {
                "intent_code": "query_order_status",
                "name": "查询订单状态",
                "description": "查询订单状态、物流状态、订单进度",
                "examples": ["帮我查下订单状态", "订单123到哪了"],
                "keywords": ["订单", "物流", "状态"],
                "agent_url": "llm://default",
                "status": "active",
                "dispatch_priority": 90,
                "primary_threshold": 0.7,
                "candidate_threshold": 0.5,
            },
        ],
        model=settings.llm_recognizer_model or settings.llm_model,
    )
    print(json.dumps({"recognition": recognition.model_dump()}, ensure_ascii=False, indent=2))

    task = Task(
        session_id="session_smoke",
        intent_code="transfer_money",
        agent_url="llm://default",
        intent_name="转账",
        intent_description="执行转账、收款账户确认、付款账户确认。",
        intent_examples=["给张三转 200 元", "帮我转账给李四"],
        confidence=0.95,
        input_context={
            "recent_messages": ["user: 帮我给张三转账"],
            "long_term_memory": ["常用收款人：张三"],
        },
        slot_memory={"payee": "张三"},
        request_schema={},
        field_mapping={},
    )
    agent_result = await client.run_agent(
        task=task,
        user_input="转 200 元",
        model=settings.llm_agent_model or settings.llm_model,
    )
    print(json.dumps({"agent": agent_result.model_dump()}, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify real router LLM runtime connectivity.")
    parser.parse_args()
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
