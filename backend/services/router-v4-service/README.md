# router-v4-service

Standalone v4 intent routing service.

This service is intentionally separate from the existing `router-service`. It owns only:

- scene recognition
- scene routing spec loading
- routing-slot hint extraction from user utterance
- execution-agent task dispatch
- router-level session and transcript tracking

It does not perform business confirmation, risk checks, limits, idempotency, or direct business API calls. Those remain in scene execution agents.

Runtime switches:

```bash
ROUTER_V4_SPEC_ROOT=/path/to/spec-root
ROUTER_V4_STATE_DIR=/path/to/router-state
ROUTER_V4_CONTEXT_MAX_CHARS=4000
ROUTER_V4_RECENT_TURNS=6
ROUTER_V4_RETRIEVAL_LIMIT=3
```

If `ROUTER_V4_STATE_DIR` is unset, the service uses in-memory session and transcript stores. If it is set, router-owned session state and transcript records are persisted under that directory.

Run locally:

```bash
python -m pip install -e backend/services/router-v4-service
python -m uvicorn router_v4_service.api.app:app --reload --port 8024
```

Health:

```bash
curl http://127.0.0.1:8024/health
```

Message:

```bash
curl -X POST http://127.0.0.1:8024/api/router/v4/message \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "sess_001",
    "message": "给张三转5000块",
    "user_profile": {"user_id": "U001"},
    "page_context": {"current_page": "home"}
  }'
```

Inspect router-owned state:

```bash
curl http://127.0.0.1:8024/api/router/v4/sessions/sess_001
```

Run focused tests:

```bash
pytest backend/tests/test_router_v4_service.py -q
```
