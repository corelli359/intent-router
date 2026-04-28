# Frontend Workspace

This frontend workspace contains:

- `apps/chat-web`: user-facing conversation app
- `packages/shared-types`: shared domain types
- `packages/api-client`: API client and SSE placeholders
- `packages/ui`: shared UI primitives

## Run locally

```bash
cd frontend
npm install
npm run dev:chat
```

Default ports:

- Chat web: `http://localhost:3000`

## Target Build

For test or target clusters, the repository can generate standalone Next.js bundles under
`../prod_target/` via:

```bash
cd ..
./scripts/build_prod_target.sh
```

Important build-time parameters:

- `CHAT_BASE_PATH`
- `ROUTER_API_EXTERNAL_PATH`
- `INGRESS_HOST`
