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

## Target Build

For test or target clusters, the repository can generate standalone Next.js bundles under
`../prod_target/` via:

```bash
cd ..
./scripts/build_prod_target.sh
```

Important build-time parameters:

- `CHAT_BASE_PATH`
- `ADMIN_BASE_PATH`
- `ROUTER_API_EXTERNAL_PATH`
- `ADMIN_API_EXTERNAL_PATH`
- `INGRESS_HOST`
