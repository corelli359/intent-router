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

Key boundary:

- Router performs intent recognition, task orchestration, and dispatch only.
- Business execution is always handled by intent agent services.
- Unmatched requests must be dispatched to a separately deployed fallback agent.

## Ingress Path Contract

Ingress must expose these stable paths:

- `/admin` -> `intent-admin-web`
- `/chat` -> `intent-chat-web`
- `/api/admin/*` -> `intent-admin-api`
- `/api/router/*` -> `intent-router-api`

Do not use `/` as chat root in the target model. Chat entry should be explicit under `/chat`.

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
- Pods install dependencies on startup because source is mounted, not image-baked.
- Router reads active intent registry from admin-owned storage and refreshes cache periodically.
- Ingress should keep sticky affinity for SSE sessions when router is scaled.
