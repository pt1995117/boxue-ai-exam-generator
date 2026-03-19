#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"
LAUNCH_PATH="/opt/homebrew/bin:/Users/panting/miniconda3/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
NPM_BIN="$(whence -p npm || true)"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(whence -p python3 || true)"
fi
if [[ -z "${PYTHON_BIN}" ]]; then PYTHON_BIN="/usr/bin/python3"; fi
if [[ -z "${NPM_BIN}" ]]; then NPM_BIN="/usr/bin/npm"; fi

BACKEND_LABEL="com.boxue.admin_api"
FRONTEND_LABEL="com.boxue.admin_web"
BACKEND_PLIST="${HOME}/Library/LaunchAgents/${BACKEND_LABEL}.plist"
FRONTEND_PLIST="${HOME}/Library/LaunchAgents/${FRONTEND_LABEL}.plist"

BACKEND_LOG="/tmp/admin_api_8600.log"
FRONTEND_LOG="/tmp/admin_web_8531.log"
KEY_FILE="${ROOT_DIR}/填写您的Key.txt"

mkdir -p "${HOME}/Library/LaunchAgents"

ensure_key_file_exists() {
  if [[ ! -f "${KEY_FILE}" ]]; then
    echo "WARN: 缺少 ${KEY_FILE}，将自动创建空文件。可在管理后台【全局Key配置】中填写。"
    : > "${KEY_FILE}"
  fi
  chmod 600 "${KEY_FILE}" 2>/dev/null || true
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
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>8531</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${LAUNCH_PATH}</string>
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

launchctl bootstrap "${GUI_DOMAIN}" "${BACKEND_PLIST}"
launchctl bootstrap "${GUI_DOMAIN}" "${FRONTEND_PLIST}"
launchctl enable "${GUI_DOMAIN}/${BACKEND_LABEL}" >/dev/null 2>&1 || true
launchctl enable "${GUI_DOMAIN}/${FRONTEND_LABEL}" >/dev/null 2>&1 || true
launchctl kickstart -k "${GUI_DOMAIN}/${BACKEND_LABEL}"
launchctl kickstart -k "${GUI_DOMAIN}/${FRONTEND_LABEL}"

sleep 1

echo "Launchd services started:"
launchctl print "${GUI_DOMAIN}/${BACKEND_LABEL}" | head -n 12 || true
launchctl print "${GUI_DOMAIN}/${FRONTEND_LABEL}" | head -n 12 || true
echo ""
echo "Backend:  http://127.0.0.1:8600/"
echo "Frontend: http://127.0.0.1:8531/"
echo ""
echo "Logs:"
echo "  ${BACKEND_LOG}"
echo "  ${FRONTEND_LOG}"
