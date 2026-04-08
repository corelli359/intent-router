from __future__ import annotations

import asyncio
import sys
from pathlib import Path


BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from router_core.domain import IntentDefinition  # noqa: E402
from router_core.v2_domain import GraphStatus  # noqa: E402
from router_core.v2_graph_builder import GraphDraftNormalizer, LLMIntentGraphBuilder, UnifiedGraphDraftPayload  # noqa: E402


class _StaticLLMClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def run_json(self, *, prompt, variables, model=None, on_delta=None):
        return self.payload


def _transfer_intent(*, confirm_policy: str = "auto") -> IntentDefinition:
    return IntentDefinition(
        intent_code="transfer_money",
        name="转账",
        description="执行转账，需要收款人、金额、收款卡号和手机号后4位。",
        examples=["给我弟弟转500"],
        keywords=["转账"],
        agent_url="http://agent.example.com/transfer",
        dispatch_priority=100,
        primary_threshold=0.75,
        candidate_threshold=0.5,
        slot_schema=[
            {
                "slot_key": "recipient_name",
                "label": "收款人",
                "description": "收款人姓名",
                "value_type": "person_name",
                "required": True,
            },
            {
                "slot_key": "amount",
                "label": "转账金额",
                "description": "本次转账金额",
                "value_type": "currency",
                "required": True,
            },
        ],
        graph_build_hints={
            "intent_scope_rule": "单次转账动作是一个 intent。",
            "planner_notes": "不要把收款人、金额等槽位拆成额外节点。",
            "confirm_policy": confirm_policy,
        },
    )


def test_graph_draft_normalizer_honors_single_node_intent_output() -> None:
    normalizer = GraphDraftNormalizer()
    result = normalizer.normalize(
        payload=UnifiedGraphDraftPayload.model_validate(
            {
                "summary": "识别到 1 个事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "transfer_money", "confidence": 0.96, "reason": "single transfer action"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "transfer_money",
                        "title": "给我弟弟转500",
                        "confidence": 0.96,
                        "source_fragment": "给我弟弟转500",
                        "slot_memory": {"recipient_name": "我弟弟", "amount": "500"},
                    }
                ],
                "edges": [],
            }
        ),
        message="给我弟弟转500",
        intents_by_code={"transfer_money": _transfer_intent()},
    )

    assert [match.intent_code for match in result.recognition.primary] == ["transfer_money"]
    assert len(result.graph.nodes) == 1
    assert result.graph.nodes[0].intent_code == "transfer_money"
    assert result.graph.status == GraphStatus.DRAFT


def test_unified_graph_builder_can_force_confirmation_from_intent_hints() -> None:
    builder = LLMIntentGraphBuilder(
        _StaticLLMClient(
            {
                "summary": "识别到 1 个高风险事项",
                "needs_confirmation": False,
                "primary_intents": [
                    {"intent_code": "transfer_money", "confidence": 0.97, "reason": "matched transfer intent"}
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "transfer_money",
                        "title": "给我媳妇儿转1000",
                        "confidence": 0.97,
                        "source_fragment": "给我媳妇儿转1000",
                        "slot_memory": {"recipient_name": "我媳妇儿", "amount": "1000"},
                    }
                ],
                "edges": [],
            }
        ),
    )

    async def run() -> None:
        result = await builder.build(
            message="给我媳妇儿转1000",
            intents=[_transfer_intent(confirm_policy="always")],
            recent_messages=[],
            long_term_memory=[],
        )

        assert len(result.graph.nodes) == 1
        assert result.graph.status == GraphStatus.WAITING_CONFIRMATION
        assert result.graph.actions[0].code == "confirm_graph"

    asyncio.run(run())
