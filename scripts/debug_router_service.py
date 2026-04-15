#!/usr/bin/env python3
"""Run the router service locally in debugger-friendly mode."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
    ROOT,
    ROOT / "backend",
    ROOT / "backend" / "services" / "router-service" / "src",
]

for path in PYTHON_PATHS:
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

import uvicorn


def parse_args() -> argparse.Namespace:
    """Parse local debug launch arguments."""
    parser = argparse.ArgumentParser(
        description="Start the router service locally for IDE/debugger attachment."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8012, help="Bind port.")
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env.local"),
        help="Router env file passed through ROUTER_ENV_FILE.",
    )
    parser.add_argument(
        "--log-level",
        default="debug",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="Uvicorn log level.",
    )
    return parser.parse_args()


def main() -> int:
    """Start uvicorn without reload so breakpoints remain attached."""
    args = parse_args()
    if args.env_file:
        os.environ.setdefault("ROUTER_ENV_FILE", args.env_file)
    from router_service.api.app import create_router_app

    uvicorn.run(
        create_router_app(),
        host=args.host,
        port=args.port,
        reload=False,
        log_level=args.log_level,
        access_log=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
