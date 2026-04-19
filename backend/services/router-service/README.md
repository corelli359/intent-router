# router-service

Canonical router API service. Owns intent recognition, graph planning, state orchestration, and agent dispatch.

## Local install

```bash
python -m pip install backend/services/router-service
```

## Local run

```bash
python -m uvicorn router_service.api.app:app --reload --port 8012
```

## Local debug run

For IDE breakpoints, avoid `--reload` and run:

```bash
python scripts/debug_router_service.py --env-file .env.local --port 8012
```

## Catalog backend modes

Router supports two catalog sources:

- `ROUTER_INTENT_CATALOG_BACKEND=database`: load intent definitions from SQL storage through the admin-compatible schema
- `ROUTER_INTENT_CATALOG_BACKEND=file`: load intent definitions from `ROUTER_INTENT_CATALOG_FILE` plus optional split overlay files

Split file mode supports these paths:

- `ROUTER_INTENT_CATALOG_FILE`: base intent definitions for recognition and dispatch metadata
- `ROUTER_INTENT_FIELD_CATALOG_FILE`: optional per-intent field catalog overlay
- `ROUTER_INTENT_SLOT_SCHEMA_FILE`: optional per-intent slot schema overlay
- `ROUTER_INTENT_GRAPH_BUILD_HINTS_FILE`: optional per-intent graph-build-hints overlay

The file mode is read-only and is intended for deployments that mount prepared catalog files instead of running sqlite/admin in the same environment. This split layout lets teams configure and validate intents first, then add slot design later.

## Package

- import root: `router_service`
- source of truth: `backend/services/router-service/src/router_service`
