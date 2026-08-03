#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"

if ! command -v git >/dev/null 2>&1; then
  echo "sensitive-check prerequisite missing: git" >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "sensitive-check prerequisite missing: python3" >&2
  exit 2
fi

exec python3 "$script_dir/harness-sensitive-scan.py" "$repo_root"
