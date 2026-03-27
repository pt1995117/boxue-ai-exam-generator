#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "${ROOT_DIR}/tools/restart_admin_launchd.sh" "${ROOT_DIR}/tools/restart_admin.sh" 2>/dev/null || true
exec "${ROOT_DIR}/tools/restart_admin_launchd.sh"
