from __future__ import annotations

import argparse
import asyncio
import json
import sys
import types
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any


def _bootstrap_python_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    router_service_src = repo_root / "backend" / "services" / "router-service" / "src"
    router_service_pkg = router_service_src / "router_service"
    python_paths = [
        router_service_src,
        repo_root,
    ]
    for path in python_paths:
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
    if "router_service" not in sys.modules:
        package = types.ModuleType("router_service")
        package.__path__ = [str(router_service_pkg)]
        sys.modules["router_service"] = package


_bootstrap_python_path()

from router_service.core.shared.domain import IntentDefinition  # noqa: E402
from router_service.core.support.llm_client import LangChainLLMClient  # noqa: E402
from router_service.core.prompts.prompt_templates import (  # noqa: E402
    DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
)
from router_service.core.recognition.recognizer import LLMIntentRecognizer, NullIntentRecognizer, RecognitionResult  # noqa: E402
from router_service.settings import Settings  # noqa: E402


DEFAULT_INTENT_PAYLOADS: list[dict[str, Any]] = [
    {
        "intent_code": "query_account_balance",
        "name": "查询账户余额",
        "description": "查询银行卡账户余额，需要卡号和手机号后4位。",
        "examples": ["帮我查一下余额", "查余额", "查询银行卡余额"],
        "agent_url": "https://agent.example.com/query_account_balance",
        "primary_threshold": 0.7,
        "candidate_threshold": 0.4,
        "slot_schema": [
            {
                "slot_key": "card_number",
                "field_code": "card_number",
                "role": "card_number",
                "label": "卡号",
                "description": "银行卡卡号",
                "semantic_definition": "需要查询余额的银行卡卡号",
                "value_type": "account_number",
                "required": True,
                "allow_from_history": True,
            },
            {
                "slot_key": "phone_last_four",
                "field_code": "phone_last_four",
                "role": "phone_last_four",
                "label": "手机号后4位",
                "description": "银行卡预留手机号后4位",
                "semantic_definition": "用于辅助校验身份的手机号后4位",
                "value_type": "phone_last4",
                "required": True,
                "allow_from_history": True,
            },
        ],
    },
    {
        "intent_code": "transfer_money",
        "name": "转账",
        "description": "执行转账，需要收款人姓名、收款卡号、手机号后4位和金额。",
        "examples": ["给张三转 200 元", "帮我转账给李四", "我要给我妈转 1000"],
        "agent_url": "https://agent.example.com/transfer_money",
        "primary_threshold": 0.7,
        "candidate_threshold": 0.4,
        "slot_schema": [
            {
                "slot_key": "recipient_name",
                "field_code": "recipient_name",
                "role": "recipient_name",
                "label": "收款人姓名",
                "description": "本次转账的收款人姓名",
                "semantic_definition": "本次转账目标收款人的姓名",
                "value_type": "person_name",
                "required": True,
            },
            {
                "slot_key": "amount",
                "field_code": "amount",
                "role": "transfer_amount",
                "label": "转账金额",
                "description": "本次转账金额",
                "semantic_definition": "本次转账实际执行金额",
                "value_type": "currency",
                "required": True,
            },
            {
                "slot_key": "recipient_card_number",
                "field_code": "recipient_card_number",
                "role": "recipient_card_number",
                "label": "收款卡号",
                "description": "收款银行卡号",
                "semantic_definition": "本次转账的目标银行卡号",
                "value_type": "account_number",
                "required": True,
            },
            {
                "slot_key": "recipient_phone_last_four",
                "field_code": "recipient_phone_last_four",
                "role": "recipient_phone_last_four",
                "label": "收款人手机号后4位",
                "description": "收款人手机号后4位",
                "semantic_definition": "用于转账核验的收款人手机号后4位",
                "value_type": "phone_last4",
                "required": True,
            },
        ],
    },
    {
        "intent_code": "query_credit_card_repayment",
        "name": "查询信用卡还款信息",
        "description": "查询信用卡应还金额、最低还款额和还款日，需要卡号和手机号后4位。",
        "examples": ["查一下我的信用卡还款信息", "我这期信用卡要还多少钱"],
        "agent_url": "https://agent.example.com/query_credit_card_repayment",
        "primary_threshold": 0.7,
        "candidate_threshold": 0.4,
        "slot_schema": [
            {
                "slot_key": "card_number",
                "field_code": "card_number",
                "role": "card_number",
                "label": "信用卡卡号",
                "description": "需要查询的信用卡卡号",
                "semantic_definition": "本次账单查询对应的信用卡卡号",
                "value_type": "account_number",
                "required": True,
                "allow_from_history": True,
            },
            {
                "slot_key": "phone_last_four",
                "field_code": "phone_last_four",
                "role": "phone_last_four",
                "label": "手机号后4位",
                "description": "信用卡绑定手机号后4位",
                "semantic_definition": "用于账单查询校验的手机号后4位",
                "value_type": "phone_last4",
                "required": True,
                "allow_from_history": True,
            },
        ],
    },
    {
        "intent_code": "pay_gas_bill",
        "name": "缴纳天然气费",
        "description": "缴纳天然气费，需要燃气户号和缴费金额。",
        "examples": ["帮我交一下天然气费", "给燃气户号 88001234 交 88 元"],
        "agent_url": "https://agent.example.com/pay_gas_bill",
        "primary_threshold": 0.7,
        "candidate_threshold": 0.4,
        "slot_schema": [
            {
                "slot_key": "gas_account_number",
                "field_code": "gas_account_number",
                "role": "gas_account_number",
                "label": "燃气户号",
                "description": "天然气缴费户号",
                "semantic_definition": "本次天然气缴费对应的户号",
                "value_type": "account_number",
                "required": True,
            },
            {
                "slot_key": "amount",
                "field_code": "amount",
                "role": "payment_amount",
                "label": "缴费金额",
                "description": "本次天然气缴费金额",
                "semantic_definition": "本次天然气缴费实际金额",
                "value_type": "currency",
                "required": True,
            },
        ],
    },
    {
        "intent_code": "exchange_forex",
        "name": "换外汇",
        "description": "执行换汇，需要源币种、目标币种和金额。",
        "examples": ["把 1000 人民币换成美元", "我想换 100 美元"],
        "agent_url": "https://agent.example.com/exchange_forex",
        "primary_threshold": 0.7,
        "candidate_threshold": 0.4,
        "slot_schema": [
            {
                "slot_key": "source_currency",
                "field_code": "source_currency",
                "role": "source_currency",
                "label": "源币种",
                "description": "换汇前币种",
                "semantic_definition": "本次换汇卖出的币种",
                "value_type": "string",
                "required": True,
            },
            {
                "slot_key": "target_currency",
                "field_code": "target_currency",
                "role": "target_currency",
                "label": "目标币种",
                "description": "换汇后币种",
                "semantic_definition": "本次换汇买入的币种",
                "value_type": "string",
                "required": True,
            },
            {
                "slot_key": "amount",
                "field_code": "amount",
                "role": "exchange_amount",
                "label": "换汇金额",
                "description": "本次换汇金额",
                "semantic_definition": "本次换汇实际金额",
                "value_type": "currency",
                "required": True,
            },
        ],
    },
]


def build_intents(intent_payloads: Sequence[Mapping[str, Any]] | None = None) -> list[IntentDefinition]:
    payloads = intent_payloads or DEFAULT_INTENT_PAYLOADS
    return [IntentDefinition(**dict(item), status="active") for item in payloads]


def load_intents_from_json(path: str | Path) -> list[IntentDefinition]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("intents json must be a list")
    return build_intents(payload)


def build_llm_recognizer(*, settings: Settings | None = None) -> tuple[Settings, LLMIntentRecognizer]:
    current_settings = settings or Settings.from_env()
    if not current_settings.llm_connection_ready or current_settings.default_llm_model is None:
        raise RuntimeError(
            "ROUTER_LLM_API_BASE_URL and ROUTER_LLM_MODEL or ROUTER_LLM_RECOGNIZER_MODEL are required."
        )

    llm_client = LangChainLLMClient(
        base_url=current_settings.llm_api_base_url or "",
        api_key=current_settings.llm_api_key,
        default_model=current_settings.default_llm_model,
        timeout_seconds=current_settings.llm_timeout_seconds,
        rate_limit_max_retries=current_settings.llm_rate_limit_max_retries,
        rate_limit_retry_delay_seconds=current_settings.llm_rate_limit_retry_delay_seconds,
        extra_headers=current_settings.llm_headers,
        structured_output_method=current_settings.llm_structured_output_method,
    )
    recognizer = LLMIntentRecognizer(
        llm_client,
        model=current_settings.llm_recognizer_model or current_settings.llm_model,
        fallback=NullIntentRecognizer(),
        system_prompt_template=current_settings.llm_recognizer_system_prompt_template or DEFAULT_RECOGNIZER_SYSTEM_PROMPT,
        human_prompt_template=current_settings.llm_recognizer_human_prompt_template or DEFAULT_RECOGNIZER_HUMAN_PROMPT,
    )
    return current_settings, recognizer


async def _stdout_delta(text: str) -> None:
    print(text, end="", flush=True)


def _encode_sse(event_name: str, payload: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def recognize_message(
    message: str,
    *,
    intents: Sequence[IntentDefinition] | None = None,
    recent_messages: Sequence[str] | None = None,
    long_term_memory: Sequence[str] | None = None,
    recognizer: LLMIntentRecognizer | None = None,
    settings: Settings | None = None,
    stream: bool = False,
) -> RecognitionResult:
    if recognizer is None:
        _, effective_recognizer = build_llm_recognizer(settings=settings)
    else:
        effective_recognizer = recognizer
    return await effective_recognizer.recognize(
        message=message,
        intents=list(intents or build_intents()),
        recent_messages=list(recent_messages or []),
        long_term_memory=list(long_term_memory or []),
        on_delta=_stdout_delta if stream else None,
    )


def recognize_message_sync(
    message: str,
    *,
    intents: Sequence[IntentDefinition] | None = None,
    recent_messages: Sequence[str] | None = None,
    long_term_memory: Sequence[str] | None = None,
    settings: Settings | None = None,
    stream: bool = False,
) -> RecognitionResult:
    return asyncio.run(
        recognize_message(
            message,
            intents=intents,
            recent_messages=recent_messages,
            long_term_memory=long_term_memory,
            settings=settings,
            stream=stream,
        )
    )


def result_to_dict(result: RecognitionResult) -> dict[str, Any]:
    return {
        "primary": [
            {
                "intent_code": item.intent_code,
                "confidence": item.confidence,
                "reason": item.reason,
            }
            for item in result.primary
        ],
        "candidates": [
            {
                "intent_code": item.intent_code,
                "confidence": item.confidence,
                "reason": item.reason,
            }
            for item in result.candidates
        ],
    }


def print_result(result: RecognitionResult) -> None:
    print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))


async def iter_recognition_stream_events(
    message: str,
    *,
    intents: Sequence[IntentDefinition] | None = None,
    recent_messages: Sequence[str] | None = None,
    long_term_memory: Sequence[str] | None = None,
    recognizer: LLMIntentRecognizer | None = None,
    settings: Settings | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    effective_intents = list(intents or build_intents())
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    ok = True

    async def on_delta(text: str) -> None:
        await event_queue.put(("delta", {"text": text}))

    async def run_recognition() -> None:
        nonlocal ok
        try:
            if recognizer is None:
                current_settings, effective_recognizer = build_llm_recognizer(settings=settings)
            else:
                current_settings = settings or Settings.from_env()
                effective_recognizer = recognizer
            await event_queue.put(
                (
                    "meta",
                    {
                        "model": current_settings.llm_recognizer_model or current_settings.llm_model,
                        "intent_count": len(effective_intents),
                        "mode": "pure_llm_intent_recognition",
                    },
                )
            )
            result = await effective_recognizer.recognize(
                message=message,
                intents=effective_intents,
                recent_messages=list(recent_messages or []),
                long_term_memory=list(long_term_memory or []),
                on_delta=on_delta,
            )
            await event_queue.put(("result", result_to_dict(result)))
        except Exception as exc:
            ok = False
            await event_queue.put(
                (
                    "error",
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
        finally:
            await event_queue.put(("done", {"ok": ok}))

    task = asyncio.create_task(run_recognition())
    try:
        while True:
            event_name, payload = await event_queue.get()
            yield event_name, payload
            if event_name == "done":
                break
        await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def iter_recognition_sse_frames(
    message: str,
    *,
    intents: Sequence[IntentDefinition] | None = None,
    recent_messages: Sequence[str] | None = None,
    long_term_memory: Sequence[str] | None = None,
    recognizer: LLMIntentRecognizer | None = None,
    settings: Settings | None = None,
) -> AsyncIterator[str]:
    async for event_name, payload in iter_recognition_stream_events(
        message,
        intents=intents,
        recent_messages=recent_messages,
        long_term_memory=long_term_memory,
        recognizer=recognizer,
        settings=settings,
    ):
        yield _encode_sse(event_name, payload)


async def print_sse_frames(
    message: str,
    *,
    intents: Sequence[IntentDefinition] | None = None,
    recent_messages: Sequence[str] | None = None,
    long_term_memory: Sequence[str] | None = None,
    settings: Settings | None = None,
) -> None:
    async for frame in iter_recognition_sse_frames(
        message,
        intents=intents,
        recent_messages=recent_messages,
        long_term_memory=long_term_memory,
        settings=settings,
    ):
        print(frame, end="", flush=True)


def create_sse_app(
    *,
    intents: Sequence[IntentDefinition] | None = None,
    settings: Settings | None = None,
):
    from fastapi import FastAPI
    from pydantic import BaseModel, Field
    from starlette.responses import StreamingResponse

    app = FastAPI(title="Intent Recognition SSE Playground")
    effective_intents = list(intents or build_intents())
    effective_settings = settings or Settings.from_env()

    class RecognizeRequest(BaseModel):
        message: str
        recent_messages: list[str] = Field(default_factory=list)
        long_term_memory: list[str] = Field(default_factory=list)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/recognize")
    async def recognize(request: RecognizeRequest) -> dict[str, Any]:
        result = await recognize_message(
            request.message,
            intents=effective_intents,
            recent_messages=request.recent_messages,
            long_term_memory=request.long_term_memory,
            settings=effective_settings,
        )
        return result_to_dict(result)

    @app.post("/recognize/stream")
    async def recognize_stream(request: RecognizeRequest) -> StreamingResponse:
        return StreamingResponse(
            iter_recognition_sse_frames(
                request.message,
                intents=effective_intents,
                recent_messages=request.recent_messages,
                long_term_memory=request.long_term_memory,
                settings=effective_settings,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _add_recognition_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--message", required=True, help="User message to recognize.")
    parser.add_argument("--intents-file", help="Optional json file containing a list of intent definitions.")
    parser.add_argument(
        "--recent-message",
        action="append",
        default=[],
        help="Recent conversation message. Can be provided multiple times.",
    )
    parser.add_argument(
        "--memory",
        action="append",
        default=[],
        help="Long-term memory item. Can be provided multiple times.",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv or raw_argv[0] not in {"once", "stream", "serve"}:
        raw_argv = ["once", *raw_argv]

    parser = argparse.ArgumentParser(
        description="Pure LLM intent recognition playground. No graph planning, no agent dispatch, no regex fallback."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    once_parser = subparsers.add_parser("once", help="Run one recognition request and print the final JSON result.")
    _add_recognition_args(once_parser)
    once_parser.add_argument("--delta", action="store_true", help="Print raw LLM delta text before the final result.")

    stream_parser = subparsers.add_parser("stream", help="Print SSE frames to stdout.")
    _add_recognition_args(stream_parser)

    serve_parser = subparsers.add_parser("serve", help="Start a standalone FastAPI app with /recognize/stream.")
    serve_parser.add_argument("--intents-file", help="Optional json file containing a list of intent definitions.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    return parser.parse_args(raw_argv)


def main() -> int:
    args = parse_args()
    if args.command == "serve":
        intents = load_intents_from_json(args.intents_file) if args.intents_file else build_intents()
        app = create_sse_app(intents=intents)
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    intents = load_intents_from_json(args.intents_file) if args.intents_file else build_intents()
    if args.command == "stream":
        asyncio.run(
            print_sse_frames(
                args.message,
                intents=intents,
                recent_messages=args.recent_message,
                long_term_memory=args.memory,
            )
        )
        return 0

    result = recognize_message_sync(
        args.message,
        intents=intents,
        recent_messages=args.recent_message,
        long_term_memory=args.memory,
        stream=args.delta,
    )
    if args.delta:
        print()
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
