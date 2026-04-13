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

## Catalog backend modes

Router supports two catalog sources:

- `ROUTER_INTENT_CATALOG_BACKEND=database`: load intent definitions from SQL storage through the admin-compatible schema
- `ROUTER_INTENT_CATALOG_BACKEND=file`: load intent definitions from `ROUTER_INTENT_CATALOG_FILE` as JSON

The file mode is read-only and is intended for deployments that mount a prepared catalog file instead of running sqlite/admin in the same environment.

## Package

- import root: `router_service`
- source of truth: `backend/services/router-service/src/router_service`
