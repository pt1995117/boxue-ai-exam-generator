#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT_DIR}"

status="$(git status --porcelain=v1)"
if [[ -z "${status}" ]]; then
  echo "[hygiene] git worktree clean"
  exit 0
fi

blocked_patterns=(
  '.local/'
  'logs/'
  'node_modules/'
  'admin-web/.vite/'
  'data/.*/audit/'
  'data/.*/mapping/'
  'data/.*/slices/'
  'data/.*/bank/'
  'data/.*/exports/'
  'data/.*/materials/uploads/'
  'data/.*/materials/references/'
)

blocked_exts=(
  '.db'
  '.sqlite'
  '.sqlite3'
  '.xlsx'
  '.xls'
  '.docx'
  '.pdf'
)

bad=0

while IFS= read -r line; do
  [[ -n "${line}" ]] || continue
  path="${line:3}"
  normalized="${path//\\//}"

  for pattern in "${blocked_patterns[@]}"; do
    if [[ "${normalized}" =~ ${pattern} ]]; then
      if [[ ${bad} -eq 0 ]]; then
        echo "[hygiene] blocked worktree artifacts detected:"
      fi
      echo "  ${line}"
      bad=1
      continue 2
    fi
  done

  for ext in "${blocked_exts[@]}"; do
    if [[ "${normalized}" == *.${ext#.} ]]; then
      if [[ ${bad} -eq 0 ]]; then
        echo "[hygiene] blocked worktree artifacts detected:"
      fi
      echo "  ${line}"
      bad=1
      continue 2
    fi
  done
done <<< "${status}"

if [[ ${bad} -ne 0 ]]; then
  echo
  echo "[hygiene] move runtime outputs under .local/ or update local exclude rules before finishing work"
  exit 1
fi

echo "[hygiene] worktree has changes, but no blocked runtime artifacts were detected"
