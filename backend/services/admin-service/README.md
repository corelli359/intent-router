# admin-service

Canonical admin API service. Owns intent registration CRUD and activation state.

## Local install

```bash
python -m pip install backend/contracts/intent-registry backend/services/admin-service
```

## Local run

```bash
python -m uvicorn admin_service.api.app:app --reload --port 8011
```

## Package

- import root: `admin_service`
- source of truth: `backend/services/admin-service/src/admin_service`
