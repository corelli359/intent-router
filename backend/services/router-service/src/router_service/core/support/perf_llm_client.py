from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from router_service.core.support.json_codec import json_loads


_PERF_TRANSFER_MESSAGE = "给小明转500元"
_PERF_TRANSFER_SLOTS = {
    "payee_name": "小明",
    "amount": "500",
}


def _extract_transfer_slots(message: str) -> dict[str, str]:
    normalized = "".join(message.split())
    if normalized != _PERF_TRANSFER_MESSAGE:
        return {}
    return dict(_PERF_TRANSFER_SLOTS)


@dataclass(slots=True)
class FastPerfLLMClient:
    """In-process perf stub that removes all outbound LLM HTTP I/O."""

    default_model: str = "fake-router-llm"

    async def run_json(
        self,
        *,
        prompt: Any,
        variables: dict[str, Any],
        model: str | None = None,
        on_delta: Any | None = None,
    ) -> Any:
        del prompt, model, on_delta
        message = str(variables.get("message") or "")
        slots = _extract_transfer_slots(message)
        if "intent_json" in variables and "existing_slot_memory_json" in variables:
            intent_payload = json_loads(str(variables["intent_json"]))
            slot_schema = intent_payload.get("slot_schema") or []
            existing = json_loads(str(variables["existing_slot_memory_json"]))
            slot_keys = {str(item.get("slot_key") or "") for item in slot_schema if isinstance(item, dict)}
            payload_slots: list[dict[str, Any]] = []
            if "payee_name" in slots and "payee_name" in slot_keys and "payee_name" not in existing:
                payload_slots.append(
                    {
                        "slot_key": "payee_name",
                        "value": slots["payee_name"],
                        "source": "user_message",
                        "source_text": message,
                        "confidence": 0.99,
                    }
                )
            if "amount" in slots and "amount" in slot_keys and "amount" not in existing:
                payload_slots.append(
                    {
                        "slot_key": "amount",
                        "value": slots["amount"],
                        "source": "user_message",
                        "source_text": message,
                        "confidence": 0.99,
                    }
                )
            return {"slots": payload_slots, "ambiguousSlotKeys": []}

        if "intents_json" in variables:
            if not slots:
                return {"matches": []}
            return {
                "matches": [
                    {
                        "intent_code": "AG_TRANS",
                        "confidence": 0.99,
                        "reason": "perf stub matched transfer intent",
                    }
                ]
            }

        if "matched_intents_json" in variables or "recognition_hint_json" in variables:
            if not slots:
                return {
                    "summary": "",
                    "needs_confirmation": False,
                    "primary_intents": [],
                    "candidate_intents": [],
                    "nodes": [],
                    "edges": [],
                }
            return {
                "summary": f"识别到事项：给{slots['payee_name']}转账 {slots['amount']} 元",
                "needs_confirmation": False,
                "primary_intents": [
                    {
                        "intent_code": "AG_TRANS",
                        "confidence": 0.99,
                        "reason": "perf stub matched transfer intent",
                    }
                ],
                "candidate_intents": [],
                "nodes": [
                    {
                        "intent_code": "AG_TRANS",
                        "title": f"给{slots['payee_name']}转账 {slots['amount']} 元",
                        "confidence": 0.99,
                        "source_fragment": message,
                        "slot_memory": slots,
                        "slot_bindings": [
                            {
                                "slot_key": "payee_name",
                                "value": slots["payee_name"],
                                "source": "user_message",
                                "source_text": message,
                                "confidence": 0.99,
                            },
                            {
                                "slot_key": "amount",
                                "value": slots["amount"],
                                "source": "user_message",
                                "source_text": message,
                                "confidence": 0.99,
                            },
                        ],
                    }
                ],
                "edges": [],
            }

        return {
            "action": "resume_current",
            "reason": "perf stub keeps current flow",
            "target_intent_code": None,
        }

    async def aclose(self) -> None:
        return None
