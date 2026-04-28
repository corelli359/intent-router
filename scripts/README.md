# MVP Validation Scripts

These scripts are lightweight integration checks for the intent-router MVP.
They are designed to run against a live backend once API routes are available.

## Environment

- Use conda env `py312`
- Export base URL if not default:

```bash
export INTENT_ROUTER_BASE_URL=http://127.0.0.1:8000
```

## Scripts

### 1) One-shot launcher

```bash
bash scripts/run_mvp_checks.sh
```

The launcher runs the current `/api/v1/message` and `/api/v1/task/completion` checks.

### 2) Real LLM runtime smoke test

This test reads router runtime env from the explicit file pointed to by `ROUTER_ENV_FILE`.
If `ROUTER_ENV_FILE` is unset, the script defaults it to the repo-root `.env.local`.

```bash
python scripts/verify_real_llm_runtime.py
```

Explicit file example:

```bash
ROUTER_ENV_FILE=/etc/intent-router/.env.local python scripts/verify_real_llm_runtime.py
```

Pytest wrapper:

```bash
RUN_REAL_LLM_TEST=1 pytest backend/tests/integration/test_real_llm_runtime_script.py
```

Router file-mode now uses the business CSV `intent_table_from_updated_screenshot.csv` as the source-of-truth.
The deploy script refreshes both sqlite and the split directory `k8s/intent/router-intent-catalog/` from that CSV before rollout.

Manual sync command:

```bash
python scripts/sync_router_intents_from_csv.py
```

This sync step will:

- uniqueify duplicate `intent_code` values
- replace the old transfer intent with `AG_TRANS` while preserving the transfer slot contract
- archive the previous split catalog under `docs/archive/router-intent-catalog-pre-csv-switch`
- rewrite the file-mode catalog used by the router ConfigMap

Current direct router helpers:

```bash
python scripts/jupyter_assistant_stream_test.py
python scripts/run_router_v1_regression_suite.py --base-url "$INTENT_ROUTER_BASE_URL"
```

Notes:

- legacy session-style verification scripts have been archived
- current helper scripts only target `/api/v1/message` and `/api/v1/task/completion`

Useful environment variables:

- `INTENT_ROUTER_BASE_URL`
- `INTENT_ROUTER_HOST_HEADER`

### 3) Assistant-style router contract verification

This script bypasses `assistant-service` and calls Router directly with the
assistant-facing request shape:

- `txt`
- `config_variables`
- `executionMode`

Default mode is `router_only`, so it validates the non-stream contract without
depending on downstream agents.

```bash
python scripts/verify_router_assistant_contract.py --base-url "$INTENT_ROUTER_BASE_URL"
```

Strict two-turn transfer demo in `router_only` mode:

```bash
python scripts/verify_router_assistant_contract.py \
  --base-url "$INTENT_ROUTER_BASE_URL" \
  --strict-demo
```

When the transfer agent is deployed and you want to validate final handover:

```bash
python scripts/verify_router_assistant_contract.py \
  --base-url "$INTENT_ROUTER_BASE_URL" \
  --execution-mode execute \
  --strict-demo
```

This script checks:

- top-level response is `ok + output`
- no `snapshot` is returned on assistant protocol
- output shape matches one of:
  - router intermediate state
  - handover result
  - failed result

### 4) Focused `/api/v1/message` regression suite

This script targets the current production router entry:

- `POST /api/v1/message`

It uses the assistant-style request body and validates the current stable
single-intent transfer cases, with emphasis on multi-turn slot continuity and
overwrite cases.

List built-in cases:

```bash
python scripts/run_router_v1_regression_suite.py --list-cases
```

Run the full suite:

```bash
python scripts/run_router_v1_regression_suite.py \
  --base-url "$INTENT_ROUTER_BASE_URL"
```

Run only one focused case:

```bash
python scripts/run_router_v1_regression_suite.py \
  --base-url "$INTENT_ROUTER_BASE_URL" \
  --case-id multi_turn_override_payee_before_amount
```

### 5) Build target-cluster frontend artifacts

This generates:

- `prod_target/chat-web`
- `prod_target/k8s/intent/*.yaml`

```bash
./scripts/build_prod_target.sh
```

Example with external prefixes:

```bash
INGRESS_HOST=test.example.com \
CHAT_BASE_PATH=/intent-test/chat \
ROUTER_API_EXTERNAL_PATH=/intent-test/api/router \
./scripts/build_prod_target.sh
```

### 6) Export additional financial intents

These scripts export or sync the extra V2 financial intents used by the multi-agent runtime:

- `query_credit_card_repayment`
- `pay_gas_bill`
- `exchange_forex`

The script now points these intents to dedicated K8s services:

- `query_credit_card_repayment` -> `intent-credit-card-agent`
- `pay_gas_bill` -> `intent-gas-bill-agent`
- `exchange_forex` -> `intent-forex-agent`

Use `scripts/export_router_intent_catalog.py` for a file catalog export, or
`scripts/sync_financial_intents_to_db.py` when the router catalog backend is sqlite.

## Pytest integration wrapper

`backend/tests/integration/test_mvp_validation_scripts.py` wraps these scripts.
It only runs when:

- `RUN_INTEGRATION=1`
