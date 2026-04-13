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

### 5) Register additional financial intents

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
