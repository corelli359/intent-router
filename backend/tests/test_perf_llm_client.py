from __future__ import annotations

import asyncio

from router_service.core.support.perf_llm_client import FastPerfLLMClient


def test_fast_perf_llm_client_returns_empty_matches_for_non_fixture_messages() -> None:
    async def run() -> None:
        client = FastPerfLLMClient()

        payload = await client.run_json(
            prompt=None,
            variables={
                "message": "给王芳转100元",
                "intents_json": "[]",
            },
        )

        assert payload == {"matches": []}

    asyncio.run(run())


def test_fast_perf_llm_client_does_not_guess_slots_for_non_fixture_messages() -> None:
    async def run() -> None:
        client = FastPerfLLMClient()

        payload = await client.run_json(
            prompt=None,
            variables={
                "message": "给王芳转100元",
                "intent_json": (
                    '{"intent_code":"AG_TRANS","slot_schema":['
                    '{"slot_key":"payee_name"},{"slot_key":"amount"}]}'
                ),
                "existing_slot_memory_json": "{}",
            },
        )

        assert payload == {"slots": [], "ambiguousSlotKeys": []}

    asyncio.run(run())


def test_fast_perf_llm_client_uses_fixed_fixture_for_exact_perf_message() -> None:
    async def run() -> None:
        client = FastPerfLLMClient()

        payload = await client.run_json(
            prompt=None,
            variables={
                "message": "给小明转500元",
                "intent_json": (
                    '{"intent_code":"AG_TRANS","slot_schema":['
                    '{"slot_key":"payee_name"},{"slot_key":"amount"}]}'
                ),
                "existing_slot_memory_json": "{}",
            },
        )

        assert payload["slots"] == [
            {
                "slot_key": "payee_name",
                "value": "小明",
                "source": "user_message",
                "source_text": "给小明转500元",
                "confidence": 0.99,
            },
            {
                "slot_key": "amount",
                "value": "500",
                "source": "user_message",
                "source_text": "给小明转500元",
                "confidence": 0.99,
            },
        ]

    asyncio.run(run())
