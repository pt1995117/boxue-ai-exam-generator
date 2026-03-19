#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_PORT=8600
FRONTEND_PORT=8522
PYTHON_BIN="$(whence -p python3 || true)"
NPM_BIN="$(whence -p npm || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="/usr/bin/python3"
fi
if [[ -z "${NPM_BIN}" ]]; then
  NPM_BIN="/usr/bin/npm"
fi

BACKEND_LOG="/tmp/admin_api_8600.log"
FRONTEND_LOG="/tmp/admin_web_8522.log"
BACKEND_PID_FILE="/tmp/admin_api_8600.pid"
FRONTEND_PID_FILE="/tmp/admin_web_8522.pid"
KEY_FILE="${ROOT_DIR}/填写您的Key.txt"

ensure_key_file_exists() {
  if [[ ! -f "${KEY_FILE}" ]]; then
    echo "WARN: 缺少 ${KEY_FILE}，将自动创建空文件。可在管理后台【全局Key配置】中填写。"
    : > "${KEY_FILE}"
  fi
  chmod 600 "${KEY_FILE}" 2>/dev/null || true
}

kill_port() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:${port} -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "Stopping processes on :${port} -> ${pids}"
    for pid in ${pids}; do
      kill "${pid}" 2>/dev/null || true
    done
    sleep 0.4
    pids="$(lsof -tiTCP:${port} -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      echo "Force killing on :${port} -> ${pids}"
      for pid in ${pids}; do
        kill -9 "${pid}" 2>/dev/null || true
      done
    fi
  fi
}

start_backend() {
  cd "${ROOT_DIR}"
  nohup "${PYTHON_BIN}" admin_api.py >"${BACKEND_LOG}" 2>&1 &
  echo $! >"${BACKEND_PID_FILE}"
}

start_frontend() {
  cd "${ROOT_DIR}"
  nohup "${NPM_BIN}" --prefix admin-web run dev -- --host 127.0.0.1 --port "${FRONTEND_PORT}" >"${FRONTEND_LOG}" 2>&1 &
  echo $! >"${FRONTEND_PID_FILE}"
}

wait_for_listen() {
  local port="$1"
  local retries=15
  while (( retries > 0 )); do
    if lsof -nP -iTCP:${port} -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
    retries=$((retries - 1))
  done
  return 1
}

wait_for_http_ok() {
  local url="$1"
  local retries=20
  while (( retries > 0 )); do
    if curl -s -m 1 -H "X-System-User: admin" "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
    retries=$((retries - 1))
  done
  return 1
}

wait_for_port_free() {
  local port="$1"
  local retries=20
  while (( retries > 0 )); do
    if ! lsof -nP -iTCP:${port} -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
    retries=$((retries - 1))
  done
  return 1
}

listening_pid() {
  local port="$1"
  lsof -tiTCP:${port} -sTCP:LISTEN 2>/dev/null | head -n 1 || true
}

echo "Restarting services in ${ROOT_DIR}"
ensure_key_file_exists
kill_port "${BACKEND_PORT}"
kill_port "${FRONTEND_PORT}"
if ! wait_for_port_free "${BACKEND_PORT}"; then
  echo "ERROR: :${BACKEND_PORT} still occupied after kill attempts"
  lsof -nP -iTCP:${BACKEND_PORT} -sTCP:LISTEN || true
  exit 1
fi
if ! wait_for_port_free "${FRONTEND_PORT}"; then
  echo "ERROR: :${FRONTEND_PORT} still occupied after kill attempts"
  lsof -nP -iTCP:${FRONTEND_PORT} -sTCP:LISTEN || true
  exit 1
fi

start_backend
BACKEND_PID="$(cat "${BACKEND_PID_FILE}" 2>/dev/null || true)"

if ! wait_for_listen "${BACKEND_PORT}"; then
  echo "ERROR: Backend failed to listen on :${BACKEND_PORT}, see ${BACKEND_LOG}"
  tail -n 80 "${BACKEND_LOG}" || true
  exit 1
fi
if ! wait_for_http_ok "http://127.0.0.1:${BACKEND_PORT}/api/tenants"; then
  echo "ERROR: Backend HTTP check failed, see ${BACKEND_LOG}"
  tail -n 80 "${BACKEND_LOG}" || true
  exit 1
fi
BACKEND_BOUND_PID="$(listening_pid "${BACKEND_PORT}")"
if [[ -z "${BACKEND_BOUND_PID}" || "${BACKEND_BOUND_PID}" != "${BACKEND_PID}" ]]; then
  echo "ERROR: Backend listener PID mismatch. started=${BACKEND_PID} listening=${BACKEND_BOUND_PID}"
  lsof -nP -iTCP:${BACKEND_PORT} -sTCP:LISTEN || true
  exit 1
fi

start_frontend
FRONTEND_PID="$(cat "${FRONTEND_PID_FILE}" 2>/dev/null || true)"
if ! wait_for_listen "${FRONTEND_PORT}"; then
  echo "ERROR: Frontend failed to listen on :${FRONTEND_PORT}, see ${FRONTEND_LOG}"
  tail -n 80 "${FRONTEND_LOG}" || true
  exit 1
fi
FRONTEND_BOUND_PID="$(listening_pid "${FRONTEND_PORT}")"
if [[ -z "${FRONTEND_BOUND_PID}" ]]; then
  echo "ERROR: Frontend listener missing. started=${FRONTEND_PID}"
  lsof -nP -iTCP:${FRONTEND_PORT} -sTCP:LISTEN || true
  exit 1
fi

echo ""
echo "Backend PID: $(cat "${BACKEND_PID_FILE}")"
echo "Frontend PID: $(cat "${FRONTEND_PID_FILE}")"
echo ""
lsof -nP -iTCP:${BACKEND_PORT} -sTCP:LISTEN || true
lsof -nP -iTCP:${FRONTEND_PORT} -sTCP:LISTEN || true
echo ""
echo "Backend URL:  http://127.0.0.1:${BACKEND_PORT}/"
echo "Frontend URL: http://127.0.0.1:${FRONTEND_PORT}/"
echo ""
echo "Logs:"
echo "  ${BACKEND_LOG}"
echo "  ${FRONTEND_LOG}"
