#!/usr/bin/env python3
"""Run end-to-end router slot smoke scenarios against a deployed environment."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    host_header: str | None = None,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if host_header:
        headers["Host"] = host_header

    request = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8").strip()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8").strip()
        raise RuntimeError(f"HTTP {exc.code} for {url}: {raw}") from exc


class RouterClient:
    def __init__(
        self,
        *,
        base_url: str,
        host_header: str | None = None,
        timeout_seconds: float = 90.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host_header = host_header
        self.timeout_seconds = timeout_seconds

    def create_session(self) -> str:
        payload = _http_json(
            "POST",
            f"{self.base_url}/api/router/v2/sessions",
            {},
            host_header=self.host_header,
            timeout_seconds=self.timeout_seconds,
        )
        return str(payload["session_id"])

    def post_message(
        self,
        session_id: str,
        *,
        content: str,
        intent_code: str | None = None,
        slot_memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        if intent_code is not None:
            payload["guidedSelection"] = {
                "selectedIntents": [
                    {
                        "intentCode": intent_code,
                        "sourceFragment": content,
                        "slotMemory": slot_memory or {},
                    }
                ]
            }
        result = _http_json(
            "POST",
            f"{self.base_url}/api/router/v2/sessions/{session_id}/messages",
            payload,
            host_header=self.host_header,
            timeout_seconds=self.timeout_seconds,
        )
        return result["snapshot"]


def _last_assistant_message(snapshot: dict[str, Any]) -> str:
    messages = snapshot.get("messages", [])
    if not messages:
        return ""
    return str(messages[-1].get("content", ""))


def _single_node(snapshot: dict[str, Any]) -> dict[str, Any]:
    graph = snapshot.get("current_graph") or {}
    nodes = graph.get("nodes", [])
    if len(nodes) != 1:
        raise AssertionError(f"expected exactly one graph node, got {len(nodes)}")
    return nodes[0]


def _assert_contains(text: str, parts: list[str], *, context: str) -> None:
    missing = [part for part in parts if part not in text]
    if missing:
        raise AssertionError(f"{context}: expected text to contain {missing}, got: {text}")


def _assert_not_contains(text: str, parts: list[str], *, context: str) -> None:
    present = [part for part in parts if part in text]
    if present:
        raise AssertionError(f"{context}: expected text to exclude {present}, got: {text}")


def _assert_equal(actual: Any, expected: Any, *, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def _assert_absent(mapping: dict[str, Any], keys: list[str], *, context: str) -> None:
    present = [key for key in keys if key in mapping]
    if present:
        raise AssertionError(f"{context}: expected keys to be absent {present}, got {mapping}")


def scenario_balance_flow(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    first = client.post_message(
        session_id,
        content="帮我查一下余额",
        intent_code="query_account_balance",
    )
    first_text = _last_assistant_message(first)
    _assert_contains(first_text, ["银行卡号", "手机号后4位"], context="balance step1 prompt")
    first_node = _single_node(first)
    _assert_equal(first_node["status"], "waiting_user_input", context="balance step1 status")
    _assert_equal(first_node["slot_memory"], {}, context="balance step1 slot_memory")

    second = client.post_message(session_id, content="6222020100043219999")
    second_text = _last_assistant_message(second)
    _assert_contains(second_text, ["手机号后4位"], context="balance step2 prompt")
    _assert_not_contains(second_text, ["银行卡号"], context="balance step2 prompt")
    second_node = _single_node(second)
    _assert_equal(second_node["slot_memory"].get("card_number"), "6222020100043219999", context="balance card")

    third = client.post_message(session_id, content="手机号后四位 6666")
    third_text = _last_assistant_message(third)
    _assert_contains(third_text, ["账户余额为 8000 元"], context="balance step3 result")
    third_node = _single_node(third)
    _assert_equal(third_node["status"], "completed", context="balance step3 status")
    _assert_equal(third_node["slot_memory"].get("phone_last_four"), "6666", context="balance phone")

    return {
        "session_id": session_id,
        "result": third_text,
        "slot_memory": third_node["slot_memory"],
    }


def scenario_transfer_flow(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    first = client.post_message(
        session_id,
        content="帮我转账",
        intent_code="transfer_money",
    )
    first_text = _last_assistant_message(first)
    _assert_contains(first_text, ["金额", "收款人姓名", "收款卡卡号/尾号"], context="transfer step1 prompt")
    first_node = _single_node(first)
    _assert_equal(first_node["status"], "waiting_user_input", context="transfer step1 status")
    _assert_equal(first_node["slot_memory"], {}, context="transfer step1 slot_memory")

    second = client.post_message(session_id, content="给王芳尾号8899那张卡")
    second_text = _last_assistant_message(second)
    _assert_contains(second_text, ["金额"], context="transfer step2 prompt")
    second_node = _single_node(second)
    _assert_equal(
        second_node["slot_memory"].get("payee_name"),
        "王芳",
        context="transfer payee_name",
    )
    _assert_equal(
        second_node["slot_memory"].get("payee_card_no"),
        "8899",
        context="transfer payee_card_no",
    )

    third = client.post_message(session_id, content="500")
    third_text = _last_assistant_message(third)
    _assert_contains(third_text, ["转账成功"], context="transfer step3 result")
    third_node = _single_node(third)
    _assert_equal(third_node["status"], "completed", context="transfer step3 status")
    _assert_equal(third_node["slot_memory"].get("amount"), "500", context="transfer amount")

    return {
        "session_id": session_id,
        "result": third_text,
        "slot_memory": third_node["slot_memory"],
    }


def scenario_transfer_history_guard(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    client.post_message(
        session_id,
        content="查一下余额",
        intent_code="query_account_balance",
    )
    client.post_message(session_id, content="6222020100043219999")
    client.post_message(session_id, content="6666")

    transfer = client.post_message(
        session_id,
        content="帮我转账",
        intent_code="transfer_money",
    )
    transfer_text = _last_assistant_message(transfer)
    _assert_contains(transfer_text, ["金额", "收款人姓名"], context="history guard prompt")
    transfer_node = _single_node(transfer)
    _assert_equal(transfer_node["slot_memory"], {}, context="history guard slot_memory")

    return {
        "session_id": session_id,
        "prompt": transfer_text,
        "slot_memory": transfer_node["slot_memory"],
    }


def scenario_gas_bill_flow(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    first = client.post_message(
        session_id,
        content="帮我交燃气费",
        intent_code="pay_gas_bill",
    )
    first_text = _last_assistant_message(first)
    _assert_contains(first_text, ["燃气户号", "缴费金额"], context="gas step1 prompt")

    second = client.post_message(session_id, content="燃气户号 88001234")
    second_text = _last_assistant_message(second)
    _assert_contains(second_text, ["缴费金额"], context="gas step2 prompt")
    _assert_not_contains(second_text, ["燃气户号"], context="gas step2 prompt")
    second_node = _single_node(second)
    _assert_equal(second_node["slot_memory"].get("gas_account_number"), "88001234", context="gas account")

    third = client.post_message(session_id, content="88元")
    third_text = _last_assistant_message(third)
    _assert_contains(third_text, ["已为燃气户号 88001234 缴费 88 元"], context="gas step3 result")
    third_node = _single_node(third)
    _assert_equal(third_node["status"], "completed", context="gas step3 status")

    return {
        "session_id": session_id,
        "result": third_text,
        "slot_memory": third_node["slot_memory"],
    }


def scenario_credit_card_flow(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    first = client.post_message(
        session_id,
        content="查一下信用卡还款",
        intent_code="query_credit_card_repayment",
    )
    first_text = _last_assistant_message(first)
    _assert_contains(first_text, ["信用卡卡号", "手机号后4位"], context="credit step1 prompt")

    second = client.post_message(session_id, content="信用卡卡号 6225888800001234567，手机号后四位 6666")
    second_text = _last_assistant_message(second)
    _assert_contains(second_text, ["应还 3200 元", "最低还款 320 元"], context="credit step2 result")
    second_node = _single_node(second)
    _assert_equal(second_node["status"], "completed", context="credit step2 status")
    _assert_equal(second_node["slot_memory"].get("card_number"), "6225888800001234567", context="credit card")
    _assert_equal(second_node["slot_memory"].get("phone_last_four"), "6666", context="credit phone")

    return {
        "session_id": session_id,
        "result": second_text,
        "slot_memory": second_node["slot_memory"],
    }


def scenario_forex_direct_flow(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    first = client.post_message(
        session_id,
        content="把1000人民币换成美元",
        intent_code="exchange_forex",
    )
    first_text = _last_assistant_message(first)
    _assert_contains(first_text, ["1000 CNY 兑换为 140.00 USD"], context="forex direct result")
    first_node = _single_node(first)
    _assert_equal(first_node["status"], "completed", context="forex direct status")
    _assert_equal(first_node["slot_memory"].get("source_currency"), "CNY", context="forex source")
    _assert_equal(first_node["slot_memory"].get("target_currency"), "USD", context="forex target")
    _assert_equal(first_node["slot_memory"].get("amount"), "1000", context="forex amount")

    return {
        "session_id": session_id,
        "result": first_text,
        "slot_memory": first_node["slot_memory"],
    }


def scenario_forex_incremental_flow(client: RouterClient) -> dict[str, Any]:
    session_id = client.create_session()

    first = client.post_message(
        session_id,
        content="我想换汇",
        intent_code="exchange_forex",
    )
    first_text = _last_assistant_message(first)
    _assert_contains(first_text, ["卖出币种", "买入币种", "换汇金额"], context="forex step1 prompt")

    second = client.post_message(session_id, content="人民币换美元")
    second_text = _last_assistant_message(second)
    _assert_contains(second_text, ["换汇金额"], context="forex step2 prompt")
    _assert_not_contains(second_text, ["卖出币种", "买入币种"], context="forex step2 prompt")
    second_node = _single_node(second)
    _assert_equal(second_node["slot_memory"].get("source_currency"), "CNY", context="forex step2 source")
    _assert_equal(second_node["slot_memory"].get("target_currency"), "USD", context="forex step2 target")

    third = client.post_message(session_id, content="换汇金额 1000")
    third_text = _last_assistant_message(third)
    _assert_contains(third_text, ["1000 CNY 兑换为 140.00 USD"], context="forex step3 result")
    third_node = _single_node(third)
    _assert_equal(third_node["status"], "completed", context="forex step3 status")

    return {
        "session_id": session_id,
        "result": third_text,
        "slot_memory": third_node["slot_memory"],
    }


SCENARIOS = {
    "balance_flow": scenario_balance_flow,
    "transfer_flow": scenario_transfer_flow,
    "transfer_history_guard": scenario_transfer_history_guard,
    "gas_bill_flow": scenario_gas_bill_flow,
    "credit_card_flow": scenario_credit_card_flow,
    "forex_direct_flow": scenario_forex_direct_flow,
    "forex_incremental_flow": scenario_forex_incremental_flow,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run router slot smoke scenarios.")
    parser.add_argument("--base-url", default="http://127.0.0.1")
    parser.add_argument("--host-header", default=None, help="Optional Host header, useful for ingress routing.")
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = RouterClient(
        base_url=args.base_url,
        host_header=args.host_header,
        timeout_seconds=args.timeout_seconds,
    )

    results: dict[str, Any] = {}
    for name, scenario in SCENARIOS.items():
        results[name] = scenario(client)
        print(f"[OK] {name}")

    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
