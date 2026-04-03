# Frontend Workspace

This frontend workspace contains:

- `apps/chat-web`: user-facing conversation app
- `apps/admin-web`: management console skeleton
- `packages/shared-types`: shared domain types
- `packages/api-client`: API client and SSE placeholders
- `packages/ui`: shared UI primitives

## Run locally

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

Default ports:

- Chat web: `http://localhost:3000`
- Admin web: `http://localhost:3001`

