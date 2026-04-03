# Minikube Deployment

This stack deploys three services and one ingress into namespace `intent`:

- `intent-backend`
- `intent-chat-web`
- `intent-admin-web`
- `intent-router` ingress

All services use mounted source code from the host via Minikube mount. The manifests expect the repository to be mounted inside the Minikube node at `/mnt/intent-router`.

## Requirements

1. Docker Desktop or another working Docker daemon
2. `minikube`
3. `kubectl`

## Start

```bash
minikube start --driver=docker
minikube addons enable ingress
```

Keep the repository mounted in a separate terminal:

```bash
minikube mount "$(pwd)":/mnt/intent-router
```

Deploy:

```bash
kubectl apply -k k8s/intent
kubectl -n intent rollout status deploy/intent-backend
kubectl -n intent rollout status deploy/intent-chat-web
kubectl -n intent rollout status deploy/intent-admin-web
kubectl -n intent get ingress intent-router
```

Access:

```bash
open http://ai.intent-router.cc
open http://ai.intent-router.cc/admin
```

## Notes

- The pods install dependencies on startup because the source is mounted rather than baked into images.
- Frontends proxy `/api/router/*` and `/api/admin/*` to `intent-backend` inside the cluster.
- Short-term memory is session scoped and expires after 30 minutes. Expired session state is promoted into customer-scoped long-term memory keyed by `cust_id`.
- Ingress uses sticky cookie affinity so one browser session keeps hitting the same pod when you scale replicas.
- If the mount stops, the pods lose access to the live source tree.
