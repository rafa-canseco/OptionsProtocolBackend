#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 {doctor|fast|full|targeted [pytest-path ...]}" >&2
  exit 2
}

mode="${1:-}"
case "$mode" in
  doctor | fast | full)
    [[ $# -eq 1 ]] || usage
    ;;
  targeted)
    [[ $# -ge 2 ]] || usage
    targeted_tests=("${@:2}")
    ;;
  *) usage ;;
esac

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
cd "$repo_root"

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" ]]; then
  echo "harness prerequisite missing: uv" >&2
  exit 2
fi
if [[ ! -f pyproject.toml || ! -f uv.lock ]]; then
  echo "harness prerequisite missing: pyproject.toml or uv.lock" >&2
  exit 2
fi

for candidate in "$repo_root/.env" "$repo_root"/.env.*; do
  [[ -e "$candidate" || -L "$candidate" ]] || continue
  [[ "$(basename "$candidate")" == ".env.example" ]] && continue
  echo "harness prerequisite failed: use a clean worktree without local environment files" >&2
  exit 2
done

runtime_dir=""
cleanup() {
  if [[ -n "${runtime_dir:-}" && -d "$runtime_dir" ]]; then
    rm -rf -- "$runtime_dir"
  fi
}
runtime_dir="$(mktemp -d "${TMPDIR:-/tmp}/options-backend-harness.XXXXXX")"
if [[ -z "$runtime_dir" || ! -d "$runtime_dir" ]]; then
  echo "harness prerequisite failed: could not create isolated runtime directory" >&2
  exit 2
fi
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$runtime_dir/home" "$runtime_dir/cache" "$runtime_dir/tmp"

clean_env=(
  env -i
  "PATH=$(dirname "$uv_bin"):/usr/local/bin:/usr/bin:/bin"
  "HOME=$runtime_dir/home"
  "TMPDIR=$runtime_dir/tmp"
  "XDG_CACHE_HOME=$runtime_dir/cache"
  "PYTHONPATH=$repo_root"
  "PYTHONDONTWRITEBYTECODE=1"
  "PYTHONHASHSEED=0"
  "TZ=UTC"
  "LC_ALL=C"
  "APP_ENV=test"
  "SUPABASE_URL=http://127.0.0.1:9"
  "SUPABASE_ANON_KEY=offline-placeholder"
  "SUPABASE_SERVICE_ROLE_KEY=offline-placeholder"
  "POSITION_CURSOR_SECRET=offline-position-cursor-placeholder"
  "RPC_URL=http://127.0.0.1:9"
  "SOLANA_USDC_MINT=11111111111111111111111111111111"
  "TOKENIZED_FUND_INDEXER_ENABLED=false"
  "FUND_NAV_REPORTER_ENABLED=false"
  "TRADE_LOG_PATH=/dev/null"
  "NO_PROXY=127.0.0.1,localhost"
  "HTTP_PROXY=http://127.0.0.1:9"
  "HTTPS_PROXY=http://127.0.0.1:9"
  "ALL_PROXY=http://127.0.0.1:9"
  "UV_OFFLINE=1"
)
if [[ -n "${GIT_INDEX_FILE:-}" ]]; then
  clean_env+=("GIT_INDEX_FILE=$GIT_INDEX_FILE")
fi

"${clean_env[@]}" "$script_dir/harness-context.sh" --check
"${clean_env[@]}" "$script_dir/harness-sensitive-check.sh"

if ! "${clean_env[@]}" "$uv_bin" lock --locked --offline >/dev/null; then
  echo "harness prerequisite failed: uv.lock is stale or unavailable offline" >&2
  exit 2
fi
if ! "${clean_env[@]}" "$uv_bin" sync --frozen --offline --dev >/dev/null; then
  echo "harness prerequisite failed: run 'uv sync --frozen --dev' before offline checks" >&2
  exit 2
fi

if [[ "$mode" == "doctor" ]]; then
  echo "harness doctor: ready"
  exit 0
fi

graphify_check=".pi/packages/graphify-balanced/test/index.check.ts"
if [[ -f "$graphify_check" ]]; then
  node_bin="$(command -v node || true)"
  if [[ -z "$node_bin" ]]; then
    echo "harness prerequisite missing: node" >&2
    exit 2
  fi
  "${clean_env[@]}" "$node_bin" --test "$graphify_check"
fi

detect_external_tests() {
  "${clean_env[@]}" python3 - "$repo_root/tests" <<'PY'
import ast
import os
import sys
from pathlib import Path


def dotted_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


tests_root = Path(sys.argv[1])
if not tests_root.is_dir():
    raise SystemExit(1)

for current, directories, filenames in os.walk(tests_root, topdown=True):
    directories[:] = [
        name
        for name in directories
        if name not in {"__pycache__", "options-scenarios"}
    ]
    for filename in filenames:
        if not filename.endswith(".py"):
            continue
        path = Path(current) / filename
        if path.is_symlink():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            print(f"full-check prerequisite failed: cannot parse test markers in {path}", file=sys.stderr)
            raise SystemExit(2) from error
        for node in ast.walk(tree):
            name = dotted_name(node)
            if name.endswith(("mark.integration", "mark.network")):
                raise SystemExit(0)

raise SystemExit(1)
PY
}

has_external_tests=false
integration_runner_relative="scripts/harness-integration.sh"
integration_runner="$repo_root/$integration_runner_relative"
if [[ "$mode" == "full" ]]; then
  if detect_external_tests; then
    has_external_tests=true
    runner_index_entry="$(
      "${clean_env[@]}" git -C "$repo_root" ls-files -s -- "$integration_runner_relative"
    )"
    if [[ "$runner_index_entry" != 100755\ * || ! -f "$integration_runner" || \
      ! -x "$integration_runner" || -L "$integration_runner" ]]; then
      echo "full-check prerequisite missing: add tracked executable scripts/harness-integration.sh for integration/network tests" >&2
      exit 2
    fi
  else
    marker_status=$?
    if [[ $marker_status -ne 1 ]]; then
      exit "$marker_status"
    fi
  fi
fi

"${clean_env[@]}" "$uv_bin" run --frozen --offline ruff format --check .
"${clean_env[@]}" "$uv_bin" run --frozen --offline ruff check .

pytest_args=(-q)
if [[ "$mode" == "fast" ]]; then
  pytest_args+=(-m "not integration and not network")
elif [[ "$mode" == "full" && "$has_external_tests" == true ]]; then
  pytest_args+=(-m "not integration and not network")
elif [[ "$mode" == "targeted" ]]; then
  pytest_args+=("${targeted_tests[@]}")
fi

"${clean_env[@]}" "$uv_bin" run --frozen --offline pytest "${pytest_args[@]}"

if [[ "$mode" == "full" && "$has_external_tests" == true ]]; then
  echo "harness full: running declared integration entrypoint"
  mkdir -p \
    "$runtime_dir/integration-home" \
    "$runtime_dir/integration-cache" \
    "$runtime_dir/integration-tmp" \
    "$runtime_dir/docker"
  integration_env=(
    env -i
    "PATH=$repo_root/.venv/bin:$(dirname "$uv_bin"):/Applications/Docker.app/Contents/Resources/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    "HOME=$runtime_dir/integration-home"
    "TMPDIR=$runtime_dir/integration-tmp"
    "XDG_CACHE_HOME=$runtime_dir/integration-cache"
    "DOCKER_CONFIG=$runtime_dir/docker"
    "PYTHONPATH=$repo_root"
    "PYTHONDONTWRITEBYTECODE=1"
    "PYTHONHASHSEED=0"
    "TZ=UTC"
    "LC_ALL=C"
    "CI=1"
    "HARNESS_INTEGRATION=1"
    "HARNESS_REPO_ROOT=$repo_root"
    "HARNESS_INTEGRATION_TMPDIR=$runtime_dir/integration-tmp"
  )
  if "${integration_env[@]}" "$integration_runner"; then
    :
  else
    runner_status=$?
    echo "harness full: integration entrypoint failed with exit $runner_status" >&2
    exit "$runner_status"
  fi
fi

echo "harness $mode: passed"
