#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 [show|--check]" >&2
  exit 2
}

mode="${1:-show}"
case "$mode" in
  show | --check) ;;
  *) usage ;;
esac

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
local_agents="$repo_root/AGENTS.md"

if [[ ! -f "$local_agents" ]]; then
  echo "context prerequisite missing: backend AGENTS.md" >&2
  exit 2
fi

workspace_root=""
context_router=""
for candidate in "$repo_root/.." "$repo_root/../.."; do
  candidate="$(cd "$candidate" 2>/dev/null && pwd -P || true)"
  if [[ -n "$candidate" && -f "$candidate/AGENTS.md" && -x "$candidate/harness/bin/context" ]]; then
    workspace_root="$candidate"
    context_router="$candidate/harness/bin/context"
    break
  fi
done

if [[ -z "$workspace_root" ]]; then
  if [[ "$mode" == "show" ]]; then
    printf '%s\n' "$local_agents"
  else
    echo "context: standalone backend protocol is available"
  fi
  exit 0
fi

if ! routed_output="$("$context_router" backend)"; then
  echo "context prerequisite failed: workspace router rejected backend" >&2
  exit 2
fi

resolved_paths=()
saw_backend_protocol=false
while IFS= read -r line; do
  [[ "$line" == "  "* ]] || continue
  relative_path="${line#  }"
  [[ -n "$relative_path" ]] || continue
  if [[ "$relative_path" == "backend/AGENTS.md" ]]; then
    resolved_path="$local_agents"
    saw_backend_protocol=true
  else
    resolved_path="$workspace_root/$relative_path"
  fi
  if [[ ! -f "$resolved_path" ]]; then
    echo "context prerequisite missing: $relative_path" >&2
    exit 2
  fi
  resolved_paths+=("$resolved_path")
done <<< "$routed_output"

if [[ ${#resolved_paths[@]} -eq 0 || "$saw_backend_protocol" != true ]]; then
  echo "context prerequisite failed: incomplete backend route" >&2
  exit 2
fi

if [[ "$mode" == "show" ]]; then
  printf '%s\n' "$workspace_root/AGENTS.md"
  printf '%s\n' "${resolved_paths[@]}"
else
  echo "context: workspace backend route is complete"
fi
