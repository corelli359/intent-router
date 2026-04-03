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

- events stream: `GET /api/router/sessions/{session_id}/events` (SSE)
- message submit: `POST /api/router/sessions/{session_id}/messages`
- expected event sequence: `task.waiting_user_input -> task.completed`

### 3) One-shot launcher

```bash
RUN_ROUTER_SSE_TEST=1 bash scripts/run_mvp_checks.sh
```

If `RUN_ROUTER_SSE_TEST` is not `1`, the SSE check is skipped.

### 4) Real LLM runtime smoke test

This test reads local router runtime env from `.env` / `.env.local` and calls the configured model directly through the backend LangChain client.

```bash
python scripts/verify_real_llm_runtime.py
```

Pytest wrapper:

```bash
RUN_REAL_LLM_TEST=1 pytest backend/tests/integration/test_real_llm_runtime_script.py
```

## Pytest integration wrapper

`backend/tests/integration/test_mvp_validation_scripts.py` wraps these scripts.
It only runs when:

- `RUN_INTEGRATION=1`
- and for SSE lifecycle test: `RUN_ROUTER_SSE_TEST=1`
