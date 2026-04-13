#!/usr/bin/env python3
"""Export the builtin router intent catalog into a JSON file for file-backed runtime mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from register_financial_intents import build_payloads


def parse_args() -> argparse.Namespace:
    """Parse the export destination for the generated catalog file."""
    parser = argparse.ArgumentParser(
        description="Export builtin finance intents as a JSON catalog file."
    )
    parser.add_argument(
        "--output",
        default="k8s/intent/router-intent-catalog.json",
        help="Path to the generated JSON catalog file.",
    )
    return parser.parse_args()


def main() -> int:
    """Write the current builtin finance intents to the configured output path."""
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_payload = {"intents": build_payloads()}
    output_path.write_text(
        json.dumps(catalog_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] exported router intent catalog: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
