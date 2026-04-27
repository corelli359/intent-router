# router-v4-service

Standalone v4 intent routing service.

This service is intentionally separate from the existing `router-service`. It owns only:

- independent intent recognition
- single markdown intent catalog loading
- skill reference dispatch
- execution-agent task dispatch
- router-level session and transcript tracking

Intent specs are centralized in one markdown source: `default_specs/intent.md`. Router loads that file for recognition and dispatch metadata. Each intent entry includes its intent boundary, target agent, dispatch contract and `skill_ref`. Router does not read the referenced Skill body; execution agents load `skills/*.skill.md` in their own lifecycle. TOML frontmatter is only the machine-readable header inside the markdown document. There are no hand-maintained JSON scene specs.

It does not perform business confirmation, risk checks, limits, idempotency, or direct business API calls. Those remain in scene execution agents.
The Router runtime also does not perform regex/keyword matching, hardcoded push acceptance, heuristic slot extraction, or business-slot clarification. Recognition is produced by the LLM recognizer from `intent.md`. Business slot extraction belongs to the selected execution Agent and its Skill.

Implemented v0.2 capabilities:

- assistant-push routing with `push_context.intents`
- direct push acceptance/rejection handling without Router confirmation
- multi-intent `planned` response with task-level stream URLs
- execution-agent structured output callback
- fixed `ishandover=true` plus `output.data=[]` handover protocol
- one-hop fallback dispatch to `fallback-agent`
- task and graph snapshots
- LLM-only recognizer path with no rules fallback in Router runtime

Runtime switches:

```bash
ROUTER_V4_SPEC_ROOT=/path/to/spec-root
ROUTER_V4_STATE_DIR=/path/to/router-state
ROUTER_V4_RECOGNIZER_BACKEND=llm
ROUTER_V4_LLM_API_BASE_URL=https://provider.example/v1
ROUTER_V4_LLM_API_KEY=...
ROUTER_V4_LLM_MODEL=...
ROUTER_V4_FALLBACK_AGENT_ID=fallback-agent
ROUTER_V4_CONTEXT_MAX_CHARS=4000
ROUTER_V4_RECENT_TURNS=6
ROUTER_V4_RETRIEVAL_LIMIT=3
```

`ROUTER_V4_*` LLM variables take precedence. For local compatibility, the service also accepts existing `ROUTER_RECOGNIZER_BACKEND` and `ROUTER_LLM_*` names.

If `ROUTER_V4_STATE_DIR` is unset, the service uses in-memory session and transcript stores. If it is set, router-owned session state and transcript records are persisted under that directory.

Run locally:

```bash
python -m pip install -e backend/services/router-v4-service
ROUTER_V4_ENV_FILE=.env.local PYTHONPATH=backend/services/router-v4-service/src \
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

Record execution-agent output:

```bash
curl -X POST http://127.0.0.1:8024/api/router/v4/agent-output \
  -H 'content-type: application/json' \
  -d '{
    "session_id": "sess_001",
    "task_id": "task_001",
    "status": "completed",
    "output": {"data": [{"type": "balance", "amount": "1000.00"}]}
  }'
```

Standalone observer UI:

```bash
cd services/router-v4-observer-ui
python -m http.server 3010 --bind 127.0.0.1
```

Open `http://127.0.0.1:3010` after the Router V4 service is running on port `8024`.

Run focused tests:

```bash
pytest backend/tests/test_router_v4_service.py -q
```
