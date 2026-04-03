#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kubectl delete -k "${ROOT_DIR}/k8s/intent" --ignore-not-found
