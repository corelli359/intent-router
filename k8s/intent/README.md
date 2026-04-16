# Minikube Deployment

This document describes the target deployment model for namespace `intent`.

## Target Topology

Control plane and runtime plane are deployed separately:

- `intent-admin-api` (Admin API, single replica by default)
- `intent-router-api` (Router API, scalable replicas)
- `intent-order-agent`
- `intent-appointment-agent`
- `intent-chat-web`
- `intent-admin-web`
- `intent-router` ingress

Current agent split:

- `intent-order-agent` handles `query_account_balance`
- `intent-appointment-agent` handles `transfer_money`
- `intent-credit-card-agent` handles `query_credit_card_repayment`
- `intent-gas-bill-agent` handles `pay_gas_bill`
- `intent-forex-agent` handles `exchange_forex`

Current source of truth:

- `intent-order-agent` starts from `backend/services/agents/account-balance-agent/src/account_balance_agent`
- `intent-appointment-agent` starts from `backend/services/agents/transfer-money-agent/src/transfer_money_agent`
- `intent-credit-card-agent` starts from `backend/services/agents/credit-card-repayment-agent/src/credit_card_repayment_agent`
- `intent-gas-bill-agent` starts from `backend/services/agents/gas-bill-agent/src/gas_bill_agent`
- `intent-forex-agent` starts from `backend/services/agents/forex-agent/src/forex_agent`

Key boundary:

- Router performs intent recognition, task orchestration, and dispatch only.
- Business execution is always handled by intent agent services.
- Unmatched requests must be dispatched to a separately deployed fallback agent.

## Ingress Path Contract

Ingress must expose these stable paths:

- `/admin` -> `intent-admin-web`
- `/chat` -> `intent-chat-web`
- `/chat/v2` -> V2 chat page inside the same `intent-chat-web`
- `/api/admin/*` -> `intent-admin-api`
- `/api/router/*` -> `intent-router-api`
- `/api/router/v2/*` -> V2 router API inside the same `intent-router-api`

Do not use `/` as chat root in the target model. Chat entry should be explicit under `/chat`.

V2 note:

- current manifests do not need a second chat-web or router-api Deployment just to expose V2
- existing prefix routing already covers `/chat/v2` via `/chat`
- existing prefix routing already covers `/api/router/v2/*` via `/api/router`
- this is the preferred rollout mode while keeping total memory lower than duplicating the full runtime plane

## Resource Requests Requirement

Every deployment must define `resources.requests`:

- `resources.requests.cpu`
- `resources.requests.memory`

Reason:

- improves scheduler predictability
- prevents bursty pods from starving other services
- creates a reliable baseline for scaling router replicas

## Operational Notes

- Source is mounted into Minikube node at `/mnt/intent-router`.
- Router runtime config is mounted from ConfigMap `intent-router-api-env` to `/etc/intent-router/.env.local`.
- Router file-mode catalog is mounted from ConfigMap `intent-router-intent-catalog` to `/etc/intent-router/catalog/`.
- The deploy script generates that ConfigMap from the repo-root `.env.local` on the mounted workspace.
- The deploy script also exports the current sqlite intent snapshot to `k8s/intent/router-intent-catalog/` and regenerates the router catalog ConfigMap before rollout.
- The repo now keeps a generated catalog ConfigMap snapshot at `k8s/intent/router-intent-catalog-configmap.yaml`.
- For non-Minikube target clusters with different external hosts or path prefixes, generate
  `prod_target/k8s/intent/*.yaml` via `scripts/build_prod_target.sh` and deploy those rendered manifests instead.
- Pods now install only their own local service package on startup.
- Deployment startup no longer depends on `backend/src` or the monorepo root package.
- New financial agents are deployed one by one instead of piggybacking on the legacy two-agent topology.
- If cluster resources become tight later, the deployment script is the place to stop after the last healthy standalone rollout.
- Router can read active intent registry either from admin-owned storage or from the mounted JSON catalog file, and refreshes cache periodically.
- Ingress should keep sticky affinity for SSE sessions when router is scaled.
