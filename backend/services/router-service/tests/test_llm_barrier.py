from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from router_service.core.recognition.recognizer import HeuristicIntentRecognizer, LLMIntentRecognizer
from router_service.core.shared.domain import IntentDefinition
from router_service.core.shared.graph_domain import GraphNodeState
from router_service.core.slots.extractor import SlotExtractor
from router_service.core.support.llm_barrier import (
    ROUTER_LLM_BARRIER_ENABLED_ENV,
    build_llm_barrier_error,
    llm_barrier_triggered,
)
from router_service.models.intent import IntentSlotDefinition
from router_service.settings import ROUTER_ENV_FILE_ENV, Settings


class _BarrierLLMClient:
    barrier_enabled = True

    async def run_json(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("barrier fallback should avoid calling run_json")


class LLMBarrierTests(unittest.TestCase):
    def test_build_llm_barrier_error_is_clear_and_detectable(self) -> None:
        error = build_llm_barrier_error(
            model="router-perf-model",
            prompt_name="ChatPromptTemplate",
            base_url="https://llm.example.internal",
        )

        self.assertTrue(llm_barrier_triggered(error))
        self.assertIn(ROUTER_LLM_BARRIER_ENABLED_ENV, str(error))
        self.assertIn("router-perf-model", str(error))
        self.assertIn("ChatPromptTemplate", str(error))

    def test_settings_reads_barrier_flag_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                ROUTER_ENV_FILE_ENV: "/tmp/router-service-does-not-exist.env",
                ROUTER_LLM_BARRIER_ENABLED_ENV: "true",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.router_llm_barrier_enabled)

    def test_barrier_recognizer_falls_back_to_heuristics(self) -> None:
        recognizer = LLMIntentRecognizer(
            _BarrierLLMClient(),
            fallback=HeuristicIntentRecognizer(),
        )
        transfer_intent = IntentDefinition(
            intent_code="AG_TRANS",
            name="立即发起一笔转账交易",
            description="实时转账交易执行。",
            domain_code="ag_trans",
            domain_name="转账服务",
            examples=["发起转账", "我要转账", "转5000元给朋友", "立即转账"],
            routing_examples=["发起转账", "我要转账", "转5000元给朋友", "立即转账"],
            agent_url="http://intent-appointment-agent.intent.svc.cluster.local:8000/api/agent/run",
            slot_schema=[
                IntentSlotDefinition(
                    slot_key="amount",
                    label="金额",
                    description="本次转账金额",
                    semantic_definition="转账动作需要执行的金额",
                    value_type="currency",
                    required=True,
                )
            ],
        )
        query_intent = IntentDefinition(
            intent_code="AG_MENU_21",
            name="查询转账相关功能",
            description="转账记录查询、常用收款人管理。",
            domain_code="ag_menu_21",
            domain_name="转账查询",
            examples=["查转账记录", "转账限额多少"],
            routing_examples=["查转账记录", "转账限额多少"],
            agent_url="http://intent-fallback-agent.intent.svc.cluster.local:8000/api/agent/run",
        )

        result = asyncio.run(
            recognizer.recognize(
                "给小明转500元",
                [transfer_intent, query_intent],
                [],
                [],
            )
        )

        self.assertEqual(result.primary[0].intent_code, "AG_TRANS")
        self.assertTrue(any(item.details.get("barrier_enabled") for item in result.diagnostics or []))

    def test_barrier_slot_extractor_keeps_transfer_name_and_amount_without_llm(self) -> None:
        extractor = SlotExtractor()
        intent = IntentDefinition(
            intent_code="AG_TRANS",
            name="立即发起一笔转账交易",
            description="实时转账交易执行。",
            domain_code="ag_trans",
            domain_name="转账服务",
            examples=["发起转账", "我要转账"],
            routing_examples=["发起转账", "我要转账"],
            agent_url="http://intent-appointment-agent.intent.svc.cluster.local:8000/api/agent/run",
            slot_schema=[
                IntentSlotDefinition(
                    slot_key="payee_name",
                    label="收款人姓名",
                    description="收款人姓名",
                    semantic_definition="当前转账的收款人姓名",
                    value_type="string",
                    required=True,
                ),
                IntentSlotDefinition(
                    slot_key="amount",
                    label="金额",
                    description="本次转账金额",
                    semantic_definition="转账动作需要执行的金额",
                    value_type="currency",
                    required=True,
                ),
            ],
        )
        node = GraphNodeState(
            intent_code="AG_TRANS",
            title="立即发起一笔转账交易",
            confidence=0.92,
        )

        result = asyncio.run(
            extractor.extract(
                intent=intent,
                node=node,
                graph_source_message="给小明转500元",
                current_message="给小明转500元",
            )
        )

        self.assertEqual(result.slot_memory, {"payee_name": "小明", "amount": "500"})


if __name__ == "__main__":
    unittest.main()
