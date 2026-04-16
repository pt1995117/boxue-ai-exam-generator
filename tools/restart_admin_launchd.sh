#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="${BOXUE_RUNTIME_DIR:-${ROOT_DIR}/.local/runtime}"
CACHE_DIR="${BOXUE_CACHE_DIR:-${ROOT_DIR}/.local/cache}"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"
LAUNCH_PATH="/opt/homebrew/bin:/Users/panting/miniconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
BACKEND_PORT=8600
FRONTEND_PORT=8522

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
NPM_BIN="$(whence -p npm || true)"

resolve_python_bin() {
  local candidate=""
  local dep_check='import flask, pandas, jieba, sentence_transformers, openpyxl, xlrd'
  for candidate in "${ROOT_DIR}/.venv/bin/python" "$(whence -p python3 || true)" "/usr/bin/python3"; do
    [[ -n "${candidate}" && -x "${candidate}" ]] || continue
    if "${candidate}" -c "${dep_check}" >/dev/null 2>&1; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(resolve_python_bin || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
  echo "ERROR: No usable Python interpreter with required mapping deps was found"
  echo "       Required: flask, pandas, jieba, sentence-transformers, openpyxl, xlrd"
  exit 1
fi
if [[ -z "${NPM_BIN}" ]]; then NPM_BIN="/usr/bin/npm"; fi

BACKEND_LABEL="com.boxue.admin_api"
FRONTEND_LABEL="com.boxue.admin_web"
BACKEND_PLIST="${HOME}/Library/LaunchAgents/${BACKEND_LABEL}.plist"
FRONTEND_PLIST="${HOME}/Library/LaunchAgents/${FRONTEND_LABEL}.plist"

BACKEND_LOG="/tmp/admin_api_8600.log"
FRONTEND_LOG="/tmp/admin_web_${FRONTEND_PORT}.log"
KEY_FILE="${BOXUE_KEY_FILE:-${RUNTIME_DIR}/config/填写您的Key.txt}"

mkdir -p "${HOME}/Library/LaunchAgents"

ensure_key_file_exists() {
  mkdir -p "$(dirname "${KEY_FILE}")" "${CACHE_DIR}"
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
    sleep 0.5
    pids="$(lsof -tiTCP:${port} -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      echo "Force killing on :${port} -> ${pids}"
      for pid in ${pids}; do
        kill -9 "${pid}" 2>/dev/null || true
      done
    fi
  fi
}

wait_for_port_free() {
  local port="$1"
  local retries=25
  while (( retries > 0 )); do
    if ! lsof -nP -iTCP:${port} -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
    retries=$((retries - 1))
  done
  return 1
}

wait_for_listen() {
  local port="$1"
  local retries=40
  while (( retries > 0 )); do
    if lsof -nP -iTCP:${port} -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
    retries=$((retries - 1))
  done
  return 1
}

wait_for_http_ok() {
  local url="$1"
  local header="${2:-}"
  local retries=40
  while (( retries > 0 )); do
    if [[ -n "${header}" ]]; then
      if curl -fsS -m 2 -H "${header}" "${url}" >/dev/null 2>&1; then
        return 0
      fi
    else
      if curl -fsS -m 2 "${url}" >/dev/null 2>&1; then
        return 0
      fi
    fi
    sleep 0.25
    retries=$((retries - 1))
  done
  return 1
}

ensure_key_file_exists

cat > "${BACKEND_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${BACKEND_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>admin_api.py</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${LAUNCH_PATH}</string>
    <key>BOXUE_RUNTIME_DIR</key><string>${RUNTIME_DIR}</string>
    <key>BOXUE_CACHE_DIR</key><string>${CACHE_DIR}</string>
    <key>BOXUE_KEY_FILE</key><string>${KEY_FILE}</string>
    <key>VITE_DISABLE_HMR</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${BACKEND_LOG}</string>
  <key>StandardErrorPath</key><string>${BACKEND_LOG}</string>
</dict>
</plist>
EOF

cat > "${FRONTEND_PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${FRONTEND_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${NPM_BIN}</string>
    <string>--prefix</string>
    <string>${ROOT_DIR}/admin-web</string>
    <string>run</string>
    <string>dev</string>
    <string>--</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>${FRONTEND_PORT}</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${LAUNCH_PATH}</string>
    <key>BOXUE_RUNTIME_DIR</key><string>${RUNTIME_DIR}</string>
    <key>BOXUE_CACHE_DIR</key><string>${CACHE_DIR}</string>
    <key>BOXUE_KEY_FILE</key><string>${KEY_FILE}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${FRONTEND_LOG}</string>
  <key>StandardErrorPath</key><string>${FRONTEND_LOG}</string>
</dict>
</plist>
EOF

launchctl bootout "${GUI_DOMAIN}" "${BACKEND_PLIST}" >/dev/null 2>&1 || true
launchctl bootout "${GUI_DOMAIN}" "${FRONTEND_PLIST}" >/dev/null 2>&1 || true
sleep 0.5

kill_port "${BACKEND_PORT}"
kill_port "${FRONTEND_PORT}"

if ! wait_for_port_free "${BACKEND_PORT}"; then
  echo "ERROR: :${BACKEND_PORT} still occupied after shutdown"
  lsof -nP -iTCP:${BACKEND_PORT} -sTCP:LISTEN || true
  exit 1
fi

if ! wait_for_port_free "${FRONTEND_PORT}"; then
  echo "ERROR: :${FRONTEND_PORT} still occupied after shutdown"
  lsof -nP -iTCP:${FRONTEND_PORT} -sTCP:LISTEN || true
  exit 1
fi

launchctl bootstrap "${GUI_DOMAIN}" "${BACKEND_PLIST}"
launchctl bootstrap "${GUI_DOMAIN}" "${FRONTEND_PLIST}"
launchctl enable "${GUI_DOMAIN}/${BACKEND_LABEL}" >/dev/null 2>&1 || true
launchctl enable "${GUI_DOMAIN}/${FRONTEND_LABEL}" >/dev/null 2>&1 || true
launchctl kickstart -k "${GUI_DOMAIN}/${BACKEND_LABEL}"
launchctl kickstart -k "${GUI_DOMAIN}/${FRONTEND_LABEL}"

if ! wait_for_listen "${BACKEND_PORT}"; then
  echo "ERROR: Backend failed to listen on :${BACKEND_PORT}, see ${BACKEND_LOG}"
  tail -n 80 "${BACKEND_LOG}" || true
  exit 1
fi

if ! wait_for_http_ok "http://127.0.0.1:${BACKEND_PORT}/api/tenants" "X-System-User: admin"; then
  echo "ERROR: Backend HTTP check failed, see ${BACKEND_LOG}"
  tail -n 80 "${BACKEND_LOG}" || true
  exit 1
fi

if ! wait_for_listen "${FRONTEND_PORT}"; then
  echo "ERROR: Frontend failed to listen on :${FRONTEND_PORT}, see ${FRONTEND_LOG}"
  tail -n 80 "${FRONTEND_LOG}" || true
  exit 1
fi

if ! wait_for_http_ok "http://127.0.0.1:${FRONTEND_PORT}/"; then
  echo "ERROR: Frontend HTTP check failed, see ${FRONTEND_LOG}"
  tail -n 80 "${FRONTEND_LOG}" || true
  exit 1
fi

echo "Launchd services started:"
launchctl print "${GUI_DOMAIN}/${BACKEND_LABEL}" | head -n 12 || true
launchctl print "${GUI_DOMAIN}/${FRONTEND_LABEL}" | head -n 12 || true
echo ""
echo "Backend:  http://127.0.0.1:${BACKEND_PORT}/"
echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}/"
echo ""
echo "Logs:"
echo "  ${BACKEND_LOG}"
echo "  ${FRONTEND_LOG}"
echo "Runtime Dir: ${RUNTIME_DIR}"
echo "Cache Dir:   ${CACHE_DIR}"
echo "Key File:    ${KEY_FILE}"
