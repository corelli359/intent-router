#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-intent}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-minikube}"
TARGET_PATH="${MINIKUBE_MOUNT_TARGET:-/mnt/intent-router}"
MOUNT_CONTAINER="${MOUNT_CONTAINER:-intent-router-mount}"
PROXY_CONTAINER="${PROXY_CONTAINER:-intent-router-ingress-http}"

minikube_cmd() {
  MINIKUBE_HOME="${MINIKUBE_HOME_ROOT:-$HOME}" minikube "$@"
}

node_kubectl() {
  local quoted=""
  printf -v quoted "%q " "$@"
  minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "KCTL=\$(echo /var/lib/minikube/binaries/*/kubectl); sudo KUBECONFIG=/var/lib/minikube/kubeconfig \"\$KCTL\" ${quoted}"
}

node_kubectl -n "${NAMESPACE}" delete ingress intent-router intent-router-chat --ignore-not-found || true
node_kubectl -n "${NAMESPACE}" delete service intent-chat-web intent-router-api intent-order-agent intent-appointment-agent intent-credit-card-agent intent-gas-bill-agent intent-forex-agent intent-backend --ignore-not-found || true
node_kubectl -n "${NAMESPACE}" delete deployment intent-chat-web intent-router-api intent-order-agent intent-appointment-agent intent-credit-card-agent intent-gas-bill-agent intent-forex-agent intent-backend --ignore-not-found || true
node_kubectl delete namespace "${NAMESPACE}" --ignore-not-found || true
docker rm -f "${PROXY_CONTAINER}" >/dev/null 2>&1 || true
docker rm -f "${MOUNT_CONTAINER}" >/dev/null 2>&1 || true
minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "sudo umount -l ${TARGET_PATH} >/dev/null 2>&1 || true" || true
