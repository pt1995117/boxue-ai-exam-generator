#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

git -C "${ROOT_DIR}" config core.hooksPath .githooks
chmod +x "${ROOT_DIR}/.githooks/pre-commit"

echo "Installed git hooks for ${ROOT_DIR}"
echo "core.hooksPath=.githooks"
