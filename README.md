# intent-router

Intent Router MVP for intent registration, intent recognition, task dispatching, and SSE task state delivery.

## Project Structure

- `backend/services/admin-service`: admin service source of truth
- `backend/services/router-service`: router service source of truth
- `backend/services/agents/account-balance-agent`: account balance agent source of truth
- `backend/services/agents/transfer-money-agent`: transfer money agent source of truth
- `backend/services/agents/credit-card-repayment-agent`: credit card repayment agent source of truth
- `backend/services/agents/gas-bill-agent`: gas bill payment agent source of truth
- `backend/services/agents/forex-agent`: forex exchange agent source of truth
- `backend/services/agents/fallback-agent`: fallback agent source of truth
- `backend/contracts/intent-registry`: shared intent registration contract models
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

Current phase note:

- This branch has completed the physical backend split.
- `admin_service`, `router_service`, and `intent_registry_contracts` are now the canonical Python packages.
- Built-in agents now have canonical per-service source trees under `backend/services/agents/*-agent/src`.
- Services are physically isolated; there is no shared legacy `backend/src` package or aggregate agent shim package.

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

- Router recognizer: `router_service.core`
- Built-in agents now live under per-service directories in `backend/services/agents/*-agent/src`.

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
- Shared field semantics can now be managed in Admin under `/api/admin/fields`; intent registration may reference these global fields through `slot_schema[].field_code`.

## Deployment Requirements

Kubernetes deployments must define resource `requests` at minimum:

- `resources.requests.cpu`
- `resources.requests.memory`

Rationale:

- predictable scheduling and memory pressure control
- safer multi-replica router scaling
- cleaner SLO isolation between admin and router workloads

## Local Development

Install backend dependencies for the monorepo regression workspace:

```bash
python -m pip install -e .[dev]
```

Run admin/router as independently installable services:

```bash
python -m pip install -e backend/contracts/intent-registry -e backend/services/admin-service -e backend/services/router-service
python -m uvicorn admin_service.api.app:app --reload --port 8011
python -m uvicorn router_service.api.app:app --reload --port 8012
```

Run built-in agents as independently installable services:

```bash
python -m pip install -e backend/services/agents/account-balance-agent
python -m pip install -e backend/services/agents/transfer-money-agent
python -m pip install -e backend/services/agents/credit-card-repayment-agent
python -m pip install -e backend/services/agents/gas-bill-agent
python -m pip install -e backend/services/agents/forex-agent
python -m pip install -e backend/services/agents/fallback-agent

python -m uvicorn account_balance_agent.app:app --reload --port 8101
python -m uvicorn transfer_money_agent.app:app --reload --port 8102
python -m uvicorn credit_card_repayment_agent.app:app --reload --port 8103
python -m uvicorn gas_bill_agent.app:app --reload --port 8104
python -m uvicorn forex_agent.app:app --reload --port 8105
python -m uvicorn fallback_agent.app:app --reload --port 8106
```

Run tests:

```bash
pytest
```

Independent deployments should install and start canonical packages directly from their service directories.

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
