# intent-router

Intent Router MVP with admin CRUD, serial task orchestration, SSE updates, and configurable real LLM / Agent connectivity.

## Structure

- `backend/`: FastAPI platform app, router core, admin API, tests
- `frontend/`: chat web, admin web, shared packages
- `docs/`: product and architecture notes
- `k8s/`: deployment manifests
- `scripts/`: local verification and cluster helper scripts
- `design/`: flow diagrams

## Setup

Install backend dependencies:

```bash
python -m pip install -e .[dev]
```

Run the backend locally:

```bash
uvicorn app:app --app-dir backend/src --reload --port 8011
```

Run tests:

```bash
pytest
```

Run the frontends:

```bash
cd frontend
npm install
npm run dev:chat
```

In another terminal:

```bash
cd frontend
npm run dev:admin
```

## Runtime LLM Wiring

Connection secrets must stay in local env files or shell env vars. This repo ignores `.env` and `.env.*` by default, so do not put secrets in committed files.

The LLM-facing paths are implemented with `langchain` async chains:

- `ChatPromptTemplate` for prompt engineering and variable injection
- `ChatOpenAI` for OpenAI-compatible model access
- structured output parsing for intent recognition and `llm://` task execution

This is router runtime config only. Admin API / Admin Web do not manage model providers, API keys, or model parameters.

1. Copy `.env.example` to `.env` or `.env.local`.
2. Fill in your provider settings:
   - `ROUTER_LLM_API_BASE_URL`
   - `ROUTER_LLM_API_KEY`
   - `ROUTER_LLM_MODEL`
   - optional `ROUTER_LLM_STRUCTURED_OUTPUT_METHOD=json_mode`
3. Enable LLM-based intent recognition with `ROUTER_RECOGNIZER_BACKEND=llm`.
4. If you want the built-in demo intents (`mock://...`) to call the real model instead of the hardcoded simulator, set `ROUTER_ENABLE_LLM_FOR_MOCK_AGENT=1`.

Supported `agent_url` modes:

- `mock://...`: existing mock behavior, or real LLM when `ROUTER_ENABLE_LLM_FOR_MOCK_AGENT=1`
- `llm://default`: execute this intent directly with the configured LangChain async model chain
- `llm://your-model-name`: execute this intent with a model override
- `https://...` / `http://...`: call an external Agent endpoint with JSON or streaming JSON/SSE responses

For external HTTP Agents, the router now assembles request payloads from `request_schema` and `field_mapping` before dispatching.

## Notes

- Admin Web only manages business-side intent metadata and dispatch targets.
- API keys stay on the backend only. Do not store them in `agent_url`, frontend code, or committed config files.
