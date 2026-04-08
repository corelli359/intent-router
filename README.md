# intent-router

Intent Router MVP for intent registration, intent recognition, task dispatching, and SSE task state delivery.

## Project Structure

- `backend/`: FastAPI services, router core, admin API, tests
- `frontend/`: chat web, admin web, shared packages
- `docs/`: product and architecture docs
- `k8s/`: deployment manifests
- `scripts/`: local verification and cluster helper scripts
- `design/`: flow diagrams

## Target Service Topology

The target architecture separates control plane and runtime plane:

- `admin-api` service:
  - owns intent registry CRUD and activation
  - single replica by default
- `router-api` service:
  - owns session/message ingress, intent recognition, and agent dispatch
  - can scale to multiple replicas
- `intent-agent-*` services:
  - one business capability per endpoint
  - fallback must also be an independent agent service

Critical boundary:

- Router only does recognition + dispatch + task state orchestration.
- Router does not execute business intent logic itself.
- When no active business intent matches, router dispatches to fallback agent.
- The default Minikube stack ships built-in demo agents for order status and appointment cancellation; register/deploy fallback separately when needed.

## Ingress Path Rules

Required ingress path conventions:

- `/admin` -> Admin Web
- `/chat` -> Chat Web
- `/chat/v2` -> Chat Web V2 entry
- `/api/admin/*` -> Admin API
- `/api/router/*` -> Router API
- `/api/router/v2/*` -> Router API V2 entry

This keeps UI routes and API routes explicit, and avoids mixing admin and chat traffic.

## V2 Dynamic Graph Runtime

The repository now ships two router experiences in parallel:

- V1: serial task queue under `/chat` and `/api/router/*`
- V2: dynamic intent graph runtime under `/chat/v2` and `/api/router/v2/*`

V2 is implemented inside the existing chat-web and router-api services instead of cloning a second full deployment set. This keeps memory usage lower while still exposing a separate versioned path for rollout.

## Runtime and LLM Wiring

Connection secrets must stay in local env files or shell env vars. This repo ignores `.env` and `.env.*` by default.

Router and agents support OpenAI-compatible model access via `langchain`:

- Router recognizer: `router_core`
- Built-in agents: `intent_agents.account_balance_app`, `intent_agents.transfer_money_app`, `intent_agents.credit_card_repayment_app`, `intent_agents.gas_bill_payment_app`, `intent_agents.forex_exchange_app`, `intent_agents.fallback_app`

Minimum runtime env:

1. Copy `.env.example` to `.env` or `.env.local`.
2. Set:
   - `ROUTER_LLM_API_BASE_URL`
   - `ROUTER_LLM_API_KEY`
   - `ROUTER_LLM_MODEL`
   - `ADMIN_REPOSITORY_BACKEND=database`
   - `ADMIN_DATABASE_URL` (SQLite or MySQL DSN)
3. Set recognizer backend with `ROUTER_RECOGNIZER_BACKEND=llm`.

Supported `agent_url`:

- `http://...` / `https://...`

Intent lifecycle:

- New intent defaults to `inactive`.
- Admin activates/deactivates intents explicitly.
- Router recognizes only active non-fallback intents.
- Fallback intent is excluded from recognizer candidates and dispatched only when no match is selected.

## Deployment Requirements

Kubernetes deployments must define resource `requests` at minimum:

- `resources.requests.cpu`
- `resources.requests.memory`

Rationale:

- predictable scheduling and memory pressure control
- safer multi-replica router scaling
- cleaner SLO isolation between admin and router workloads

## Local Development

Install backend dependencies:

```bash
python -m pip install -e .[dev]
```

Run split backend services:

```bash
uvicorn admin_entry:app --app-dir backend/src --reload --port 8011
uvicorn router_entry:app --app-dir backend/src --reload --port 8012
```

Run built-in agents:

```bash
uvicorn intent_agents.account_balance_app:app --app-dir backend/src --reload --port 8101
uvicorn intent_agents.transfer_money_app:app --app-dir backend/src --reload --port 8102
uvicorn intent_agents.credit_card_repayment_app:app --app-dir backend/src --reload --port 8103
uvicorn intent_agents.gas_bill_payment_app:app --app-dir backend/src --reload --port 8104
uvicorn intent_agents.forex_exchange_app:app --app-dir backend/src --reload --port 8105
uvicorn intent_agents.fallback_app:app --app-dir backend/src --reload --port 8106
```

Run tests:

```bash
pytest
```

Compatibility note:

- `backend/src/app.py` still exists as an aggregate app for local integration tests.
- Deployment entrypoints should use `admin_entry:app` and `router_entry:app`.

Run frontends:

```bash
cd frontend
npm install
npm run dev:chat
npm run dev:admin
```

Chat entries after startup:

- V1: `http://127.0.0.1:3000/chat`
- V2: `http://127.0.0.1:3000/chat/v2`
