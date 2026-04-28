#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${INTENT_ROUTER_BASE_URL:-http://127.0.0.1:8000}"

python scripts/verify_admin_intents.py --base-url "${BASE_URL}"

if [[ "${RUN_ROUTER_CONTRACT_TEST:-0}" == "1" ]]; then
  python scripts/verify_router_assistant_contract.py --base-url "${BASE_URL}" --strict-demo
else
  echo "[SKIP] Router contract check disabled. Set RUN_ROUTER_CONTRACT_TEST=1 to enable."
fi

echo "[DONE] MVP validation scripts completed."
