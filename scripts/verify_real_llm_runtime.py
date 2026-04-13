from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
    ROOT / "backend" / "services" / "router-service" / "src",
]
for path in PYTHON_PATHS:
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from router_service.settings import Settings  # noqa: E402
from router_service.core.support.llm_client import LangChainLLMClient  # noqa: E402
from router_service.core.shared.domain import IntentDefinition  # noqa: E402
from router_service.core.recognition.recognizer import LLMIntentRecognizer  # noqa: E402


def _masked_key(api_key: str | None) -> str | None:
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


async def _run() -> None:
    os_env_file = ROOT / ".env.local"

    os.environ.setdefault("ROUTER_ENV_FILE", str(os_env_file))
    settings = Settings.from_env()
    if not settings.llm_connection_ready or not settings.llm_api_key:
        raise RuntimeError("ROUTER_LLM_* env is incomplete. Please fill the file pointed to by ROUTER_ENV_FILE.")

    client = LangChainLLMClient(
        base_url=settings.llm_api_base_url or "",
        api_key=settings.llm_api_key,
        default_model=settings.default_llm_model or "",
        timeout_seconds=settings.llm_timeout_seconds,
        rate_limit_max_retries=settings.llm_rate_limit_max_retries,
        rate_limit_retry_delay_seconds=settings.llm_rate_limit_retry_delay_seconds,
        extra_headers=settings.llm_headers,
        structured_output_method=settings.llm_structured_output_method,
    )

    print(
        json.dumps(
            {
                "config": {
                    "base_url": settings.llm_api_base_url,
                    "model": settings.default_llm_model,
                    "recognizer_backend": settings.recognizer_backend,
                    "structured_output_method": settings.llm_structured_output_method,
                    "api_key_masked": _masked_key(settings.llm_api_key),
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    recognizer = LLMIntentRecognizer(client, model=settings.default_llm_model)
    recognition = await recognizer.recognize(
        message="帮我给张三转 200 元，顺便查一下订单 123 的状态",
        recent_messages=["user: 你好", "assistant: 你好，请问需要什么帮助"],
        long_term_memory=["常用收款人：张三"],
        intents=[
            IntentDefinition(
                intent_code="transfer_money",
                name="转账",
                description="转账、付款账户确认、转账结果查询",
                examples=["给张三转 200 元", "帮我转账给李四"],
                keywords=["转账", "付款", "收款人"],
                agent_url="https://agent.example.com/transfer",
                status="active",
                dispatch_priority=100,
                primary_threshold=0.7,
                candidate_threshold=0.5,
            ),
            IntentDefinition(
                intent_code="query_order_status",
                name="查询订单状态",
                description="查询订单状态、物流状态、订单进度",
                examples=["帮我查下订单状态", "订单 123 到哪了"],
                keywords=["订单", "物流", "状态"],
                agent_url="https://agent.example.com/order",
                status="active",
                dispatch_priority=90,
                primary_threshold=0.7,
                candidate_threshold=0.5,
            ),
        ],
    )
    print(json.dumps({"recognition": recognition.model_dump()}, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify real router recognizer LLM connectivity.")
    parser.parse_args()
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
