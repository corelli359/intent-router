#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-intent}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-minikube}"
MINIKUBE_HOME_ROOT="${MINIKUBE_HOME_ROOT:-$HOME}"
MINIKUBE_DIR="${MINIKUBE_DIR:-${MINIKUBE_HOME_ROOT}/.minikube}"
MINIKUBE_BIN="${MINIKUBE_BIN:-$(command -v minikube)}"
TARGET_PATH="${MINIKUBE_MOUNT_TARGET:-/mnt/intent-router}"
MOUNT_CONTAINER="${MOUNT_CONTAINER:-intent-router-mount}"
PROXY_CONTAINER="${PROXY_CONTAINER:-intent-router-ingress-http}"
INGRESS_HOST="${INGRESS_HOST:-intent-router.kkrrc-359.top}"
RUNNER_IMAGE="${RUNNER_IMAGE:-$(docker inspect "${MINIKUBE_PROFILE}" --format '{{.Config.Image}}')}"
INTENT_CATALOG_SOURCE_DIR="${INTENT_CATALOG_SOURCE_DIR:-${ROOT_DIR}/k8s/intent/router-intent-catalog}"

minikube_cmd() {
  MINIKUBE_HOME="${MINIKUBE_HOME_ROOT}" minikube "$@"
}

node_kubectl() {
  local quoted=""
  printf -v quoted "%q " "$@"
  minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "KCTL=\$(echo /var/lib/minikube/binaries/*/kubectl); sudo KUBECONFIG=/var/lib/minikube/kubeconfig \"\$KCTL\" ${quoted}"
}

start_mount_container() {
  minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "sudo umount -l ${TARGET_PATH} >/dev/null 2>&1 || true; sudo mkdir -p ${TARGET_PATH}"
  docker rm -f "${MOUNT_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${MOUNT_CONTAINER}" \
    --restart unless-stopped \
    --network host \
    -e HOME="${MINIKUBE_HOME_ROOT}" \
    -e MINIKUBE_HOME="${MINIKUBE_HOME_ROOT}" \
    -v "${MINIKUBE_BIN}:/usr/local/bin/minikube:ro" \
    -v "${MINIKUBE_DIR}:${MINIKUBE_DIR}" \
    -v "${ROOT_DIR}:/workspace" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    --entrypoint bash \
    "${RUNNER_IMAGE}" \
    -lc "exec /usr/local/bin/minikube mount /workspace:${TARGET_PATH}" >/dev/null
}

wait_for_mount() {
  local attempt
  for attempt in $(seq 1 30); do
    if minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "ls ${TARGET_PATH}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Minikube mount did not become ready at ${TARGET_PATH}" >&2
  return 1
}

start_ingress_proxy() {
  local minikube_ip
  minikube_ip="$(minikube_cmd ip --profile "${MINIKUBE_PROFILE}")"
  docker rm -f "${PROXY_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${PROXY_CONTAINER}" \
    --restart unless-stopped \
    --network host \
    --entrypoint bash \
    "${RUNNER_IMAGE}" \
    -lc "exec socat TCP-LISTEN:80,reuseaddr,fork TCP:${minikube_ip}:80" >/dev/null
}

ensure_ingress() {
  if node_kubectl -n ingress-nginx get deployment ingress-nginx-controller >/dev/null 2>&1; then
    return 0
  fi
  minikube_cmd addons enable ingress >/dev/null
}

ensure_ingress
node_kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=5m || true

python "${ROOT_DIR}/scripts/sync_router_intents_from_csv.py" \
  --catalog-dir "${INTENT_CATALOG_SOURCE_DIR}" \
  --output-dir "${INTENT_CATALOG_SOURCE_DIR}"

start_mount_container
wait_for_mount
node_kubectl -n "${NAMESPACE}" delete ingress intent-router-chat --ignore-not-found || true
node_kubectl -n "${NAMESPACE}" delete service intent-backend --ignore-not-found || true
node_kubectl -n "${NAMESPACE}" delete deployment intent-backend --ignore-not-found || true

minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "
  set -e
  if [ ! -f '${TARGET_PATH}/.env.local' ]; then
    echo 'Missing ${TARGET_PATH}/.env.local for router ConfigMap generation' >&2
    exit 1
  fi
  KCTL=\$(echo /var/lib/minikube/binaries/*/kubectl)
  sudo KUBECONFIG=/var/lib/minikube/kubeconfig \"\$KCTL\" -n '${NAMESPACE}' create configmap intent-router-api-env \
    --from-file=.env.local='${TARGET_PATH}/.env.local' \
    --dry-run=client -o yaml | sudo KUBECONFIG=/var/lib/minikube/kubeconfig \"\$KCTL\" apply -f -
"

minikube_cmd ssh --profile "${MINIKUBE_PROFILE}" "
  set -e
  if [ ! -d '${TARGET_PATH}/k8s/intent/router-intent-catalog' ]; then
    echo 'Missing ${TARGET_PATH}/k8s/intent/router-intent-catalog for router intent catalog ConfigMap generation' >&2
    exit 1
  fi
  KCTL=\$(echo /var/lib/minikube/binaries/*/kubectl)
  sudo KUBECONFIG=/var/lib/minikube/kubeconfig \"\$KCTL\" -n '${NAMESPACE}' create configmap intent-router-intent-catalog \
    --from-file='${TARGET_PATH}/k8s/intent/router-intent-catalog' \
    --dry-run=client -o yaml | sudo KUBECONFIG=/var/lib/minikube/kubeconfig \"\$KCTL\" apply -f -
"

manifests=(
  namespace.yaml
  router-api.yaml
  admin-api.yaml
  order-agent.yaml
  appointment-agent.yaml
  credit-card-repayment-agent.yaml
  gas-bill-agent.yaml
  forex-agent.yaml
  fallback-agent.yaml
  chat-web.yaml
  admin-web.yaml
  ingress.yaml
)

for manifest in "${manifests[@]}"; do
  node_kubectl apply -f "${TARGET_PATH}/k8s/intent/${manifest}"
done

for deployment in \
  intent-router-api \
  intent-admin-api \
  intent-order-agent \
  intent-appointment-agent \
  intent-credit-card-agent \
  intent-gas-bill-agent \
  intent-forex-agent \
  intent-fallback-agent \
  intent-chat-web \
  intent-admin-web
do
  node_kubectl -n "${NAMESPACE}" rollout restart deployment/"${deployment}"
  node_kubectl -n "${NAMESPACE}" rollout status deployment/"${deployment}" --timeout=20m
done
start_ingress_proxy

echo
echo "Ingress:"
node_kubectl -n "${NAMESPACE}" get ingress
echo "Chat:  http://${INGRESS_HOST}/chat"
echo "Admin: http://${INGRESS_HOST}/admin"
