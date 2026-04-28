#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${INTENT_ROUTER_BASE_URL:-http://127.0.0.1:8000}"

python scripts/verify_router_assistant_contract.py --base-url "${BASE_URL}" --strict-demo
python scripts/run_router_v1_regression_suite.py --base-url "${BASE_URL}"

echo "[DONE] MVP validation scripts completed."
