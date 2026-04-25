from __future__ import annotations

from typing import Any
import uuid

import httpx


class CapabilityError(RuntimeError):
    """Raised when a Skill attempts a capability that was not granted."""


class ApiTool:
    """Controlled API caller backed by the request's capability map."""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    def call(
        self,
        *,
        capability: str,
        endpoint: str | None,
        body: dict[str, Any],
        user_profile: dict[str, Any],
    ) -> dict[str, Any]:
        if not endpoint:
            raise CapabilityError(f"capability endpoint is not provided: {capability}")
        if endpoint.startswith("mock://"):
            return self._mock_call(capability=capability, endpoint=endpoint, body=body, user_profile=user_profile)
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(endpoint, json=body)
                response.raise_for_status()
                payload = response.json()
                return payload if isinstance(payload, dict) else {"ok": True, "data": payload}
        raise CapabilityError(f"unsupported endpoint scheme for capability: {capability}")

    def _mock_call(
        self,
        *,
        capability: str,
        endpoint: str,
        body: dict[str, Any],
        user_profile: dict[str, Any],
    ) -> dict[str, Any]:
        if capability == "risk_check":
            amount = body.get("amount")
            balance = user_profile.get("available_balance")
            if isinstance(amount, (int, float)) and isinstance(balance, (int, float)) and amount > balance:
                return {
                    "ok": False,
                    "error_code": "insufficient_balance",
                    "message": "available balance is lower than transfer amount",
                    "available_balance": balance,
                }
            return {"ok": True, "risk_level": "pass", "endpoint": endpoint}
        if capability == "transfer":
            seed = f"{body.get('recipient')}:{body.get('amount')}"
            transaction_id = "TXN-" + uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:10].upper()
            return {"ok": True, "transaction_id": transaction_id, "endpoint": endpoint}
        return {"ok": True, "endpoint": endpoint}
