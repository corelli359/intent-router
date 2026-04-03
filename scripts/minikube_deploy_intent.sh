#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-intent}"

minikube addons enable ingress >/dev/null
kubectl apply -k "${ROOT_DIR}/k8s/intent"
kubectl -n "${NAMESPACE}" rollout status deploy/intent-backend
kubectl -n "${NAMESPACE}" rollout status deploy/intent-chat-web
kubectl -n "${NAMESPACE}" rollout status deploy/intent-admin-web
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller || true

echo
echo "Ingress:"
kubectl -n "${NAMESPACE}" get ingress intent-router
echo "Chat:  http://ai.intent-router.cc"
echo "Admin: http://ai.intent-router.cc/admin"
