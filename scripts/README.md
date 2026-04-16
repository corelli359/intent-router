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

### 1) Admin intent validation

```bash
python scripts/verify_admin_intents.py --base-url "$INTENT_ROUTER_BASE_URL"
```

Default assumptions:

- create: `POST /api/admin/intents`
- list: `GET /api/admin/intents`
- optional full CRUD paths can be overridden by flags

### 2) Router SSE lifecycle validation

```bash
python scripts/verify_router_lifecycle.py --base-url "$INTENT_ROUTER_BASE_URL"
```

Default assumptions:

- events stream: `GET /api/router/v2/sessions/{session_id}/events` (SSE)
- message submit: `POST /api/router/v2/sessions/{session_id}/messages`
- default scenario: transfer flow with first-turn partial slots, then follow-up slot补充
- expected event sequence: `session.waiting_user_input -> node.completed`

### 3) One-shot launcher

```bash
RUN_ROUTER_SSE_TEST=1 bash scripts/run_mvp_checks.sh
```

If `RUN_ROUTER_SSE_TEST` is not `1`, the SSE check is skipped.

### 4) Real LLM runtime smoke test

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

### 5) Analyze-only understanding verification

This calls the router without dispatching intent agents, so you can inspect:

- recognized primary intents
- candidate intents
- compiled graph
- per-node slot memory
- conditional edges

```bash
python scripts/verify_router_understanding.py --base-url "$INTENT_ROUTER_BASE_URL"
```

Intent-only verification:

```bash
python scripts/verify_router_understanding.py --base-url "$INTENT_ROUTER_BASE_URL" --analysis-mode intent_only
```

Direct no-arg intent-only smoke script:

```bash
python scripts/analyze_intent_only.py
```

Router file-mode export now writes the split directory `k8s/intent/router-intent-catalog/`, and the Minikube deploy script refreshes that directory from the current sqlite snapshot before rollout.

Before that export, the deploy script now runs:

```bash
python scripts/sync_financial_intents_to_db.py
```

This keeps the sqlite snapshot aligned with the latest builtin finance intent definitions before the file-mode catalog ConfigMap is regenerated.

Creative transfer multiturn intent+slot replay:

```bash
python scripts/verify_transfer_multiturn_dataset.py
```

Default dataset:

- `docs/examples/transfer_money_multiturn_cases.csv`
- answer fields come first, then `user_turn_*`, then the merged `dialogue_text`

Standard multi-turn intent + slot verification suite:

```bash
python scripts/verify_multiturn_intent_slot_suite.py
```

Default case file:

- `docs/examples/multiturn_intent_slot_cases.json`

Useful environment variables:

- `INTENT_ROUTER_BASE_URL`
- `INTENT_ROUTER_HOST_HEADER`
- `INTENT_ROUTER_CUST_ID`
- `INTENT_ROUTER_TIMEOUT_SECONDS`
- `INTENT_ROUTER_STANDARD_CASES`
- `INTENT_ROUTER_ANALYZE_BEFORE_EXECUTE`
- `INTENT_ROUTER_ANALYSIS_MODE`
- `INTENT_ROUTER_CASE_IDS`
- `INTENT_ROUTER_CASE_LIMIT`

Recommended usage:

- keep standard regression cases in the JSON file
- each turn can separately assert analyze-stage recognition and execute-stage prompt/slot state
- use this script when you want to validate multi-turn required-slot补齐能力，不只是单轮提槽
- the script is sequential and defaults to `INTENT_ROUTER_CASE_LIMIT=1`, so one run only executes one case unless you explicitly raise the limit

### 6) Build target-cluster frontend artifacts

This generates:

- `prod_target/chat-web`
- `prod_target/admin-web`
- `prod_target/k8s/intent/*.yaml`

```bash
./scripts/build_prod_target.sh
```

Example with external prefixes:

```bash
INGRESS_HOST=test.example.com \
CHAT_BASE_PATH=/intent-test/chat \
ADMIN_BASE_PATH=/intent-test/admin \
ROUTER_API_EXTERNAL_PATH=/intent-test/api/router \
ADMIN_API_EXTERNAL_PATH=/intent-test/api/admin \
./scripts/build_prod_target.sh
```

### 7) Register additional financial intents

This upserts the extra V2 financial intents used by the multi-agent runtime:

- `query_credit_card_repayment`
- `pay_gas_bill`
- `exchange_forex`

The script now points these intents to dedicated K8s services:

- `query_credit_card_repayment` -> `intent-credit-card-agent`
- `pay_gas_bill` -> `intent-gas-bill-agent`
- `exchange_forex` -> `intent-forex-agent`

```bash
python scripts/register_financial_intents.py --base-url "$INTENT_ROUTER_BASE_URL" --activate
```

## Pytest integration wrapper

`backend/tests/integration/test_mvp_validation_scripts.py` wraps these scripts.
It only runs when:

- `RUN_INTEGRATION=1`
- and for SSE lifecycle test: `RUN_ROUTER_SSE_TEST=1`
