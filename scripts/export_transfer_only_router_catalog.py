#!/usr/bin/env python3
"""Export a transfer-only split router catalog for local regression and perf tests."""

from __future__ import annotations

import argparse
from pathlib import Path

from register_financial_intents import build_payloads
from router_catalog_files import write_split_catalog


def parse_args() -> argparse.Namespace:
    """Parse the output directory and optional local agent URL override."""
    parser = argparse.ArgumentParser(
        description="Export a transfer-only router catalog."
    )
    parser.add_argument(
        "--output-dir",
        default=".local-data/router-transfer-only-catalog",
        help="Directory where the generated split catalog files will be written.",
    )
    parser.add_argument(
        "--agent-url",
        default="http://127.0.0.1:8021/api/agent/run",
        help="Transfer agent URL written into the exported catalog.",
    )
    return parser.parse_args()


def main() -> int:
    """Write the transfer-only split catalog used by local regression runs."""
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    transfer_payload = next(
        payload
        for payload in build_payloads()
        if str(payload.get("intent_code")) == "transfer_money"
    )
    transfer_payload = dict(transfer_payload)
    transfer_payload["agent_url"] = args.agent_url
    write_split_catalog(output_dir, intents=[transfer_payload])
    print(f"[OK] exported transfer-only router catalog: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
