#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"
SOURCE_K8S_DIR="${ROOT_DIR}/k8s/intent"
TARGET_DIR="${TARGET_DIR:-${ROOT_DIR}/prod_target}"
TARGET_K8S_DIR="${TARGET_DIR}/k8s/intent"

NAMESPACE="${NAMESPACE:-intent}"
INGRESS_HOST="${INGRESS_HOST:-intent-router.kkrrc-359.top}"
MOUNT_ROOT="${TARGET_MOUNT_ROOT:-/mnt/intent-router}"
ROUTER_API_ORIGIN="${ROUTER_API_ORIGIN:-http://intent-router-api.${NAMESPACE}.svc.cluster.local:8000}"
ADMIN_API_ORIGIN="${ADMIN_API_ORIGIN:-http://intent-admin-api.${NAMESPACE}.svc.cluster.local:8000}"

normalize_path() {
  local raw="${1:-}"
  if [[ -z "${raw}" ]]; then
    echo "/"
    return 0
  fi
  if [[ "${raw}" != /* ]]; then
    raw="/${raw}"
  fi
  if [[ "${raw}" != "/" ]]; then
    raw="${raw%/}"
  fi
  echo "${raw}"
}

CHAT_BASE_PATH="$(normalize_path "${CHAT_BASE_PATH:-/chat}")"
ADMIN_BASE_PATH="$(normalize_path "${ADMIN_BASE_PATH:-/admin}")"
ROUTER_API_EXTERNAL_PATH="$(normalize_path "${ROUTER_API_EXTERNAL_PATH:-/api/router}")"
ADMIN_API_EXTERNAL_PATH="$(normalize_path "${ADMIN_API_EXTERNAL_PATH:-/api/admin}")"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

copy_app_bundle() {
  local app_name="$1"
  local app_dir="${FRONTEND_DIR}/apps/${app_name}"
  local standalone_root="${app_dir}/.next/standalone"
  local target_app_dir="${TARGET_DIR}/${app_name}"
  local server_path
  local server_dir

  server_path="$(find "${standalone_root}" -type f -name server.js | head -n 1)"
  if [[ -z "${server_path}" ]]; then
    echo "Could not locate standalone server.js for ${app_name}" >&2
    exit 1
  fi
  server_dir="$(dirname "${server_path}")"

  rm -rf "${target_app_dir}"
  mkdir -p "${target_app_dir}"
  cp -R "${server_dir}/." "${target_app_dir}/"
  mkdir -p "${target_app_dir}/.next"
  cp -R "${app_dir}/.next/static" "${target_app_dir}/.next/static"
  if [[ -d "${app_dir}/public" ]]; then
    mkdir -p "${target_app_dir}/public"
    cp -R "${app_dir}/public/." "${target_app_dir}/public/"
  fi
}

build_chat_web() {
  (
    cd "${FRONTEND_DIR}"
    NEXT_TELEMETRY_DISABLED=1 \
    INTENT_CHAT_BASE_PATH="${CHAT_BASE_PATH}" \
    INTENT_ROUTER_API_ORIGIN="${ROUTER_API_ORIGIN}" \
    NEXT_PUBLIC_ROUTER_BASE_URL="${CHAT_BASE_PATH}/api/router" \
    npm run build --workspace @intent-router/chat-web
  )
  copy_app_bundle "chat-web"
}

build_admin_web() {
  (
    cd "${FRONTEND_DIR}"
    NEXT_TELEMETRY_DISABLED=1 \
    INTENT_ADMIN_BASE_PATH="${ADMIN_BASE_PATH}" \
    INTENT_ADMIN_API_ORIGIN="${ADMIN_API_ORIGIN}" \
    NEXT_PUBLIC_ADMIN_BASE_URL="${ADMIN_BASE_PATH}/api/admin" \
    npm run build --workspace @intent-router/admin-web
  )
  copy_app_bundle "admin-web"
}

copy_backend_manifests() {
  mkdir -p "${TARGET_K8S_DIR}"
  cp "${SOURCE_K8S_DIR}/namespace.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/router-api.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/admin-api.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/order-agent.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/appointment-agent.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/credit-card-repayment-agent.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/gas-bill-agent.yaml" "${TARGET_K8S_DIR}/"
  cp "${SOURCE_K8S_DIR}/forex-agent.yaml" "${TARGET_K8S_DIR}/"
}

render_web_deployment() {
  local app_name="$1"
  local service_name="$2"
  local port="$3"
  local base_path="$4"
  local output_file="$5"

  cat <<EOF > "${output_file}"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${service_name}
  namespace: ${NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${service_name}
  template:
    metadata:
      labels:
        app: ${service_name}
    spec:
      containers:
        - name: ${app_name}
          image: node:20-bookworm-slim
          imagePullPolicy: IfNotPresent
          workingDir: /workspace/prod_target/${app_name}
          env:
            - name: PORT
              value: "${port}"
            - name: HOSTNAME
              value: "0.0.0.0"
            - name: NEXT_TELEMETRY_DISABLED
              value: "1"
          command:
            - /bin/sh
            - -lc
          args:
            - |
              node server.js
          ports:
            - containerPort: ${port}
              name: http
          resources:
            requests:
              cpu: 200m
              memory: 320Mi
          startupProbe:
            httpGet:
              path: ${base_path}
              port: http
            failureThreshold: 90
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: ${base_path}
              port: http
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: ${base_path}
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
          volumeMounts:
            - name: repo
              mountPath: /workspace
      volumes:
        - name: repo
          hostPath:
            path: ${MOUNT_ROOT}
            type: Directory
---
apiVersion: v1
kind: Service
metadata:
  name: ${service_name}
  namespace: ${NAMESPACE}
spec:
  selector:
    app: ${service_name}
  ports:
    - name: http
      port: ${port}
      targetPort: http
  type: ClusterIP
EOF
}

render_web_ingress() {
  cat <<EOF > "${TARGET_K8S_DIR}/ingress-web.yaml"
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: intent-router-web
  namespace: ${NAMESPACE}
  annotations:
    nginx.ingress.kubernetes.io/affinity: "cookie"
    nginx.ingress.kubernetes.io/affinity-mode: "persistent"
    nginx.ingress.kubernetes.io/session-cookie-name: "ROUTER_SESSION"
    nginx.ingress.kubernetes.io/session-cookie-max-age: "1800"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/app-root: ${CHAT_BASE_PATH}
spec:
  ingressClassName: nginx
  rules:
    - host: ${INGRESS_HOST}
      http:
        paths:
          - path: ${ADMIN_BASE_PATH}
            pathType: Prefix
            backend:
              service:
                name: intent-admin-web
                port:
                  number: 3001
          - path: ${CHAT_BASE_PATH}
            pathType: Prefix
            backend:
              service:
                name: intent-chat-web
                port:
                  number: 3000
EOF
}

render_api_ingress() {
  local name="$1"
  local external_path="$2"
  local internal_path="$3"
  local service_name="$4"
  local output_file="$5"

  if [[ "${external_path}" == "${internal_path}" ]]; then
    cat <<EOF > "${output_file}"
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${name}
  namespace: ${NAMESPACE}
spec:
  ingressClassName: nginx
  rules:
    - host: ${INGRESS_HOST}
      http:
        paths:
          - path: ${external_path}
            pathType: Prefix
            backend:
              service:
                name: ${service_name}
                port:
                  number: 8000
EOF
    return 0
  fi

  cat <<EOF > "${output_file}"
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${name}
  namespace: ${NAMESPACE}
  annotations:
    nginx.ingress.kubernetes.io/use-regex: "true"
    nginx.ingress.kubernetes.io/rewrite-target: ${internal_path}/\$2
spec:
  ingressClassName: nginx
  rules:
    - host: ${INGRESS_HOST}
      http:
        paths:
          - path: ${external_path}(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: ${service_name}
                port:
                  number: 8000
EOF
}

render_kustomization() {
  cat <<EOF > "${TARGET_K8S_DIR}/kustomization.yaml"
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - router-api.yaml
  - admin-api.yaml
  - order-agent.yaml
  - appointment-agent.yaml
  - credit-card-repayment-agent.yaml
  - gas-bill-agent.yaml
  - forex-agent.yaml
  - chat-web.yaml
  - admin-web.yaml
  - ingress-web.yaml
  - ingress-router-api.yaml
  - ingress-admin-api.yaml
EOF
}

render_build_metadata() {
  cat <<EOF > "${TARGET_DIR}/build-info.env"
NAMESPACE=${NAMESPACE}
INGRESS_HOST=${INGRESS_HOST}
CHAT_BASE_PATH=${CHAT_BASE_PATH}
ADMIN_BASE_PATH=${ADMIN_BASE_PATH}
ROUTER_API_EXTERNAL_PATH=${ROUTER_API_EXTERNAL_PATH}
ADMIN_API_EXTERNAL_PATH=${ADMIN_API_EXTERNAL_PATH}
ROUTER_API_ORIGIN=${ROUTER_API_ORIGIN}
ADMIN_API_ORIGIN=${ADMIN_API_ORIGIN}
TARGET_MOUNT_ROOT=${MOUNT_ROOT}
BUILD_AT_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOF
}

main() {
  require_command npm
  rm -rf "${TARGET_K8S_DIR}"
  mkdir -p "${TARGET_DIR}"

  build_chat_web
  build_admin_web
  copy_backend_manifests
  render_web_deployment "chat-web" "intent-chat-web" "3000" "${CHAT_BASE_PATH}" "${TARGET_K8S_DIR}/chat-web.yaml"
  render_web_deployment "admin-web" "intent-admin-web" "3001" "${ADMIN_BASE_PATH}" "${TARGET_K8S_DIR}/admin-web.yaml"
  render_web_ingress
  render_api_ingress \
    "intent-router-router-api" \
    "${ROUTER_API_EXTERNAL_PATH}" \
    "/api/router" \
    "intent-router-api" \
    "${TARGET_K8S_DIR}/ingress-router-api.yaml"
  render_api_ingress \
    "intent-router-admin-api" \
    "${ADMIN_API_EXTERNAL_PATH}" \
    "/api/admin" \
    "intent-admin-api" \
    "${TARGET_K8S_DIR}/ingress-admin-api.yaml"
  render_kustomization
  render_build_metadata

  echo "prod_target generated at ${TARGET_DIR}"
  echo "chat base path: ${CHAT_BASE_PATH}"
  echo "admin base path: ${ADMIN_BASE_PATH}"
  echo "router api path: ${ROUTER_API_EXTERNAL_PATH}"
  echo "admin api path: ${ADMIN_API_EXTERNAL_PATH}"
}

main "$@"
