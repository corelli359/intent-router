#!/usr/bin/env python3
"""Replay creative transfer dialogues and verify intent recognition plus slot completion."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("INTENT_ROUTER_BASE_URL", "http://intent-router.kkrrc-359.top")
HOST_HEADER = os.getenv("INTENT_ROUTER_HOST_HEADER")
CUST_ID = os.getenv("INTENT_ROUTER_CUST_ID", "cust_demo")
TIMEOUT_SECONDS = float(os.getenv("INTENT_ROUTER_TIMEOUT_SECONDS", "90"))
DATASET_PATH = Path(
    os.getenv(
        "TRANSFER_MULTITURN_DATASET",
        str(ROOT / "docs" / "examples" / "transfer_money_multiturn_cases.csv"),
    )
)
MAX_CONCURRENCY = max(1, int(os.getenv("TRANSFER_MULTITURN_CONCURRENCY", "1")))

EXPECTED_SLOT_COLUMNS = {
    "expected_amount": "amount",
    "expected_ccy": "ccy",
    "expected_payer_card_no": "payer_card_no",
    "expected_payer_card_remark": "payer_card_remark",
    "expected_payee_name": "payee_name",
    "expected_payee_card_no": "payee_card_no",
    "expected_payee_card_remark": "payee_card_remark",
    "expected_payee_card_bank": "payee_card_bank",
    "expected_payee_phone": "payee_phone",
}


def _request_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send one JSON request and decode the JSON response."""
    headers = {"Content-Type": "application/json"}
    if HOST_HEADER:
        headers["Host"] = HOST_HEADER
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} calling {url}: {body}") from exc


class RouterClient:
    """Small direct client for the deployed router session APIs."""

    def __init__(self, *, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def create_session(self, *, cust_id: str) -> str:
        """Create one router session and return its id."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions",
            payload={"cust_id": cust_id},
        )
        return str(payload["session_id"])

    def post_message(self, *, session_id: str, content: str, cust_id: str) -> dict[str, Any]:
        """Submit one user message turn to the execute API."""
        payload = _request_json(
            method="POST",
            url=f"{self.base_url}/api/router/v2/sessions/{session_id}/messages",
            payload={"content": content, "cust_id": cust_id},
        )
        return payload["snapshot"]


def _load_cases(dataset_path: Path) -> list[dict[str, str]]:
    """Load the multiturn CSV dataset."""
    with dataset_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _turns(row: dict[str, str]) -> list[str]:
    """Return the non-empty user turns for one case in order."""
    turn_columns = sorted(
        (key for key in row.keys() if key.startswith("user_turn_")),
        key=lambda item: int(item.removeprefix("user_turn_")),
    )
    return [row[column].strip() for column in turn_columns if row.get(column, "").strip()]


def _graph_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return current graph nodes from one router snapshot."""
    graph = snapshot.get("current_graph") or {}
    nodes = graph.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _last_assistant_message(snapshot: dict[str, Any]) -> str:
    """Return the most recent assistant message content, if present."""
    messages = snapshot.get("messages") or []
    for item in reversed(messages):
        if item.get("role") == "assistant":
            return str(item.get("content", ""))
    return ""


def _expected_slots(row: dict[str, str]) -> dict[str, str]:
    """Project non-empty expected CSV fields into router slot keys."""
    expected: dict[str, str] = {}
    for column, slot_key in EXPECTED_SLOT_COLUMNS.items():
        value = row.get(column, "").strip()
        if value:
            expected[slot_key] = value
    return expected


def _format_failure(
    *,
    row: dict[str, str],
    session_id: str,
    final_snapshot: dict[str, Any] | None,
    reason: str,
) -> str:
    """Build a detailed failure payload for one dataset row."""
    return json.dumps(
        {
            "case_id": row.get("case_id"),
            "reason": reason,
            "session_id": session_id,
            "dialogue_text": row.get("dialogue_text", ""),
            "final_snapshot": final_snapshot,
        },
        ensure_ascii=False,
        indent=2,
    )


def _verify_case(client: RouterClient, row: dict[str, str]) -> dict[str, Any]:
    """Replay one dataset row and assert the final intent plus slots."""
    session_id = client.create_session(cust_id=CUST_ID)
    first_node_intent: str | None = None
    final_snapshot: dict[str, Any] | None = None
    for turn in _turns(row):
        final_snapshot = client.post_message(session_id=session_id, content=turn, cust_id=CUST_ID)
        nodes = _graph_nodes(final_snapshot)
        if not nodes:
            raise AssertionError(
                _format_failure(
                    row=row,
                    session_id=session_id,
                    final_snapshot=final_snapshot,
                    reason="router returned no graph nodes",
                )
            )
        if len(nodes) != 1:
            raise AssertionError(
                _format_failure(
                    row=row,
                    session_id=session_id,
                    final_snapshot=final_snapshot,
                    reason=f"expected exactly one graph node, got {len(nodes)}",
                )
            )
        if first_node_intent is None:
            first_node_intent = str(nodes[0].get("intent_code", ""))
    if final_snapshot is None:
        raise AssertionError(
            _format_failure(
                row=row,
                session_id=session_id,
                final_snapshot=final_snapshot,
                reason="dataset row has no user turns",
            )
        )

    nodes = _graph_nodes(final_snapshot)
    if len(nodes) != 1:
        raise AssertionError(
            _format_failure(
                row=row,
                session_id=session_id,
                final_snapshot=final_snapshot,
                reason=f"expected exactly one final graph node, got {len(nodes)}",
            )
        )
    final_node = nodes[0]
    expected_intent_code = row["expected_intent_code"].strip()
    if first_node_intent != expected_intent_code:
        raise AssertionError(
            _format_failure(
                row=row,
                session_id=session_id,
                final_snapshot=final_snapshot,
                reason=f"first turn intent mismatch: expected {expected_intent_code}, got {first_node_intent}",
            )
        )
    if str(final_node.get("intent_code", "")) != expected_intent_code:
        raise AssertionError(
            _format_failure(
                row=row,
                session_id=session_id,
                final_snapshot=final_snapshot,
                reason=(
                    f"final intent mismatch: expected {expected_intent_code}, "
                    f"got {final_node.get('intent_code')}"
                ),
            )
        )
    if str(final_node.get("status", "")) != "completed":
        raise AssertionError(
            _format_failure(
                row=row,
                session_id=session_id,
                final_snapshot=final_snapshot,
                reason=f"final node status is not completed: {final_node.get('status')}",
            )
        )

    actual_slots = final_node.get("slot_memory") or {}
    expected_slots = _expected_slots(row)
    for slot_key, expected_value in expected_slots.items():
        actual_value = str(actual_slots.get(slot_key, "")).strip()
        if actual_value != expected_value:
            raise AssertionError(
                _format_failure(
                    row=row,
                    session_id=session_id,
                    final_snapshot=final_snapshot,
                    reason=f"slot mismatch for {slot_key}: expected {expected_value}, got {actual_value}",
                )
            )

    return {
        "case_id": row.get("case_id"),
        "session_id": session_id,
        "intent_code": final_node.get("intent_code"),
        "status": final_node.get("status"),
        "slot_memory": actual_slots,
        "assistant_message": _last_assistant_message(final_snapshot),
    }


def main() -> int:
    """Run the full transfer multiturn dataset against the deployed router."""
    client = RouterClient(base_url=BASE_URL)
    cases = _load_cases(DATASET_PATH)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    if MAX_CONCURRENCY == 1:
        for row in cases:
            try:
                result = _verify_case(client, row)
                results.append(result)
                print(f"[OK] {row.get('case_id')} -> {result['slot_memory']}")
            except Exception as exc:
                failures.append(str(exc))
                print(f"[FAIL] {row.get('case_id')}")
    else:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
            future_map = {
                executor.submit(_verify_case, client, row): row.get("case_id")
                for row in cases
            }
            for future in as_completed(future_map):
                case_id = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                    print(f"[OK] {case_id} -> {result['slot_memory']}")
                except Exception as exc:
                    failures.append(str(exc))
                    print(f"[FAIL] {case_id}")

    print(
        json.dumps(
            {
                "base_url": BASE_URL,
                "dataset_path": str(DATASET_PATH),
                "max_concurrency": MAX_CONCURRENCY,
                "case_count": len(cases),
                "passed_count": len(results),
                "failed_count": len(failures),
                "results": results,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
