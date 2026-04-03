#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${INTENT_ROUTER_BASE_URL:-http://127.0.0.1:8000}"

python scripts/verify_admin_intents.py --base-url "${BASE_URL}"

if [[ "${RUN_ROUTER_SSE_TEST:-0}" == "1" ]]; then
  python scripts/verify_router_lifecycle.py --base-url "${BASE_URL}"
else
  echo "[SKIP] Router SSE lifecycle check disabled. Set RUN_ROUTER_SSE_TEST=1 to enable."
fi

echo "[DONE] MVP validation scripts completed."

