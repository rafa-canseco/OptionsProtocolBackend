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

if [[ "$mode" == "full" ]] && "${clean_env[@]}" git -C "$repo_root" grep --untracked -qE \
  'pytest\.mark\.(integration|network)' -- tests; then
  echo "full-check prerequisite missing: declare isolated integration services before running marked tests" >&2
  exit 2
fi

"${clean_env[@]}" "$uv_bin" run --frozen --offline ruff format --check .
"${clean_env[@]}" "$uv_bin" run --frozen --offline ruff check .

pytest_args=(-q)
if [[ "$mode" == "fast" ]]; then
  pytest_args+=(-m "not integration and not network")
elif [[ "$mode" == "targeted" ]]; then
  pytest_args+=("${targeted_tests[@]}")
fi

"${clean_env[@]}" "$uv_bin" run --frozen --offline pytest "${pytest_args[@]}"
echo "harness $mode: passed"
