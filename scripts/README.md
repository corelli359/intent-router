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

This script now supports two modes.

Interactive mode:

- it creates a session
- by default it waits for your terminal input and sends one real dialog turn at a time
- validates the returned reply, current intent, current slots, and dialog stage
- it does not call any removed analyze endpoint
- it sends `executionMode=router_only`, so the router stops before downstream agent execution

Scenario-suite mode:

- use `INTENT_ROUTER_INTERACTIVE=0` or `--scenario ...`
- runs fixed one-turn, two-turn, and three-turn contract cases
- verifies each turn against the expected intent, stage, slot set, and assistant reply
- verifies that the final turn has the complete required slots

Useful environment variables:

- `INTENT_ROUTER_BASE_URL`
- `INTENT_ROUTER_HOST_HEADER`
- `INTENT_ROUTER_CUST_ID`
- `INTENT_ROUTER_TIMEOUT_SECONDS`
- `INTENT_ROUTER_INTERACTIVE`

Recommended usage:

- run the script directly, then type each user turn in the terminal
- each turn prints only user input, assistant reply, current intent, current slots, and current stage
- use this script when you want to validate the real multi-turn intent recognition + slot filling chain without running downstream agents

List built-in scenario names:

```bash
python scripts/verify_multiturn_intent_slot_suite.py --list-scenarios
```

Run the full fixed scenario suite:

```bash
INTENT_ROUTER_INTERACTIVE=0 python scripts/verify_multiturn_intent_slot_suite.py
```

Run only a two-turn contract case such as "give name first, then amount":

```bash
INTENT_ROUTER_INTERACTIVE=0 python scripts/verify_multiturn_intent_slot_suite.py \
  --scenario two_turn_name_then_amount
```

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
