from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from router_service.core.support.json_codec import json_loads


_TRANSFER_RE = re.compile(r"(?:给|向)(?P<recipient>[\u4e00-\u9fffA-Za-z]{1,16}).{0,8}?(?P<amount>\d+(?:\.\d+)?)")
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _extract_transfer_slots(message: str) -> dict[str, str]:
    match = _TRANSFER_RE.search(message)
    if match is not None:
        return {
            "payee_name": match.group("recipient"),
            "amount": match.group("amount"),
        }
    amount_match = _AMOUNT_RE.search(message)
    return {
        "payee_name": "小明",
        "amount": amount_match.group(1) if amount_match is not None else "500",
    }


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
        if "intent_json" in variables and "existing_slot_memory_json" in variables:
            slots = _extract_transfer_slots(message)
            intent_payload = json_loads(str(variables["intent_json"]))
            slot_schema = intent_payload.get("slot_schema") or []
            existing = json_loads(str(variables["existing_slot_memory_json"]))
            slot_keys = {str(item.get("slot_key") or "") for item in slot_schema if isinstance(item, dict)}
            payload_slots: list[dict[str, Any]] = []
            if "payee_name" in slot_keys and "payee_name" not in existing:
                payload_slots.append(
                    {
                        "slot_key": "payee_name",
                        "value": slots["payee_name"],
                        "source": "user_message",
                        "source_text": message,
                        "confidence": 0.99,
                    }
                )
            if "amount" in slot_keys and "amount" not in existing:
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
            slots = _extract_transfer_slots(message)
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
