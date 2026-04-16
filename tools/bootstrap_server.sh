#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NODE_REQUIRED_MAJOR=20
RUNTIME_DIR="${BOXUE_RUNTIME_DIR:-${ROOT_DIR}/.local/runtime}"
CACHE_DIR="${BOXUE_CACHE_DIR:-${ROOT_DIR}/.local/cache}"
KEY_FILE="${BOXUE_KEY_FILE:-${RUNTIME_DIR}/config/填写您的Key.txt}"

echo "[bootstrap] root=${ROOT_DIR}"

check_node() {
  if ! command -v node >/dev/null 2>&1; then
    echo "[bootstrap][error] node is not installed"
    exit 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "[bootstrap][error] npm is not installed"
    exit 1
  fi
  local node_major
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [[ "${node_major}" != "${NODE_REQUIRED_MAJOR}" ]]; then
    echo "[bootstrap][error] unsupported node=$(node -v), require major ${NODE_REQUIRED_MAJOR}.x"
    echo "[bootstrap][hint] run: nvm use"
    exit 1
  fi
  echo "[bootstrap] node=$(node -v) npm=$(npm -v)"
}

bootstrap_python() {
  cd "${ROOT_DIR}"
  if [[ ! -d ".venv" ]]; then
    python3 -m venv .venv
  fi
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements.txt
}

ensure_key_file() {
  mkdir -p "$(dirname "${KEY_FILE}")" "${CACHE_DIR}"
  if [[ ! -f "${KEY_FILE}" ]]; then
    echo "[bootstrap][error] missing required key file: ${KEY_FILE}"
    echo "[bootstrap][hint] copy 填写您的Key.txt.example to ${KEY_FILE} and fill valid keys"
    exit 1
  fi
  chmod 600 "${KEY_FILE}" 2>/dev/null || true
}

bootstrap_frontend() {
  cd "${ROOT_DIR}/admin-web"
  rm -rf node_modules
  npm ci
  if [[ ! -f "node_modules/vite/dist/node/cli.js" ]]; then
    echo "[bootstrap][error] vite cli missing after npm ci"
    exit 1
  fi
  echo "[bootstrap] frontend dependencies ready"
}

check_node
ensure_key_file
bootstrap_python
bootstrap_frontend

echo "[bootstrap] done"
echo "[bootstrap] runtime=${RUNTIME_DIR}"
echo "[bootstrap] cache=${CACHE_DIR}"
echo "[bootstrap] key=${KEY_FILE}"
echo "[bootstrap] start backend: ${ROOT_DIR}/.venv/bin/python ${ROOT_DIR}/admin_api.py"
echo "[bootstrap] start frontend: npm --prefix ${ROOT_DIR}/admin-web run dev -- --host 127.0.0.1 --port 8522"
