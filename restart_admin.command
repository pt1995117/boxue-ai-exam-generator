#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${ROOT_DIR}/tools/restart_admin_launchd.sh" "${ROOT_DIR}/tools/restart_admin.sh" 2>/dev/null || true
if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
  exec "${ROOT_DIR}/tools/restart_admin_launchd.sh"
fi
exec "${ROOT_DIR}/tools/restart_admin.sh"
