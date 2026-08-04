#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
"$script_dir/test-b1n423-postgrest.sh"
"$script_dir/test-b1n430-postgrest.sh"
"$script_dir/test-b1n432-postgrest.sh"
