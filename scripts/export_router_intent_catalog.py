#!/usr/bin/env python3
"""Export the builtin router intent catalog into a JSON file for file-backed runtime mode."""

from __future__ import annotations

import argparse
from pathlib import Path

from register_financial_intents import build_payloads
from router_catalog_files import write_split_catalog


def parse_args() -> argparse.Namespace:
    """Parse the export destination for the generated split catalog files."""
    parser = argparse.ArgumentParser(
        description="Export builtin finance intents as split JSON catalog files."
    )
    parser.add_argument(
        "--output-dir",
        default="k8s/intent/router-intent-catalog",
        help="Directory where the generated split catalog files will be written.",
    )
    return parser.parse_args()


def main() -> int:
    """Write the current builtin finance intents to the configured split catalog directory."""
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    write_split_catalog(
        output_dir,
        intents=build_payloads(),
    )
    print(f"[OK] exported router intent catalog: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
