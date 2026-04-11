# router-service

Canonical router API service. Owns intent recognition, graph planning, state orchestration, and agent dispatch.

## Local install

```bash
python -m pip install backend/contracts/intent-registry backend/services/router-service
```

## Local run

```bash
python -m uvicorn router_service.api.app:app --reload --port 8012
```

## Package

- import root: `router_service`
- source of truth: `backend/services/router-service/src/router_service`
