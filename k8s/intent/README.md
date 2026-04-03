# Minikube Deployment

This stack deploys three services and one ingress into namespace `intent`:

- `intent-backend`
- `intent-chat-web`
- `intent-admin-web`
- `intent-router` ingress

All services use mounted source code from the host via Minikube mount. The manifests expect the repository to be mounted inside the Minikube node at `/mnt/intent-router`.

## Requirements

1. Docker or another working Docker daemon
2. `minikube`

## Start

```bash
minikube start --driver=docker
bash scripts/minikube_deploy_intent.sh
```

The deploy script will:

- enable the Minikube ingress addon
- keep the repository mounted into the node through a persistent Docker helper container
- apply the Kustomize stack from `/mnt/intent-router/k8s/intent`
- expose host port `80` to the Minikube ingress IP through a lightweight `socat` helper container

Access:

```bash
open http://intent-router.kkrrc-359.top
open http://intent-router.kkrrc-359.top/admin
```

Delete:

```bash
bash scripts/minikube_delete_intent.sh
```

## Notes

- The pods install dependencies on startup because the source is mounted rather than baked into images.
- Frontends proxy `/api/router/*` and `/api/admin/*` to `intent-backend` inside the cluster.
- Short-term memory is session scoped and expires after 30 minutes. Expired session state is promoted into customer-scoped long-term memory keyed by `cust_id`.
- Ingress uses sticky cookie affinity so one browser session keeps hitting the same pod when you scale replicas.
- If the mount stops, the pods lose access to the live source tree.
- The ingress host in this repository is `intent-router.kkrrc-359.top`.
- `scripts/minikube_deploy_intent.sh` assumes Minikube is running with the Docker driver and that Docker can bind host port `80`.
