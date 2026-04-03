#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_PATH="${MINIKUBE_MOUNT_TARGET:-/mnt/intent-router}"

echo "Mounting ${ROOT_DIR} -> ${TARGET_PATH}"
exec minikube mount "${ROOT_DIR}:${TARGET_PATH}"

