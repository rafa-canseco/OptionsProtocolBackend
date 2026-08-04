#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="${HARNESS_REPO_ROOT:-$(cd "$script_dir/.." && pwd -P)}"
runtime_root="${HARNESS_INTEGRATION_TMPDIR:-${TMPDIR:-/tmp}}"
python_bin="$repo_root/.venv/bin/python"
uv_bin="$(command -v uv || true)"

postgres_image="postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
postgrest_image="postgrest/postgrest:v12.2.12@sha256:5f4ce744539bbba786b4e24dbbd95bdb2a956dcf568c5374995a0ff4a68f5bd2"
run_suffix="$$"
network_name="b1n430-network-$run_suffix"
postgres_name="b1n430-postgres-$run_suffix"
postgrest_name="b1n430-postgrest-$run_suffix"

if [[ -z "$uv_bin" || ! -x "$python_bin" ]]; then
  echo "b1n430 integration prerequisite missing: uv environment" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "b1n430 integration prerequisite missing: docker" >&2
  exit 2
fi

cleanup() {
  docker rm -f "$postgrest_name" "$postgres_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

database_password="$($python_bin -c 'import secrets; print(secrets.token_hex(18))')"
authenticator_password="$($python_bin -c 'import secrets; print(secrets.token_hex(18))')"
jwt_secret="$($python_bin -c 'import secrets; print(secrets.token_hex(32))')"
cursor_secret="$($python_bin -c 'import secrets; print(secrets.token_hex(32))')"

make_token() {
  local role="$1"
  "$python_bin" - "$jwt_secret" "$role" <<'PY'
import base64
import hashlib
import hmac
import json
import sys
import time


def encode(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


secret, role = sys.argv[1:]
header = encode({"alg": "HS256", "typ": "JWT"})
payload = encode({"role": role, "exp": int(time.time()) + 3600})
signature = hmac.new(
    secret.encode(),
    f"{header}.{payload}".encode(),
    hashlib.sha256,
).digest()
encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
print(f"{header}.{payload}.{encoded_signature}")
PY
}

anon_token="$(make_token anon)"
authenticated_token="$(make_token authenticated)"
service_token="$(make_token service_role)"
database_uri="postgresql://authenticator"
database_uri+=":${authenticator_password}@postgres:5432/options"

docker network create "$network_name" >/dev/null
docker run --detach --rm \
  --name "$postgres_name" \
  --network "$network_name" \
  --network-alias postgres \
  --env "POSTGRES_DB=options" \
  --env "POSTGRES_PASSWORD=$database_password" \
  --env "POSTGRES_USER=postgres" \
  "$postgres_image" >/dev/null

postgres_ready=false
for _attempt in $(seq 1 60); do
  if docker exec "$postgres_name" pg_isready -U postgres -d options >/dev/null 2>&1; then
    postgres_ready=true
    break
  fi
  sleep 1
done
if [[ "$postgres_ready" != true ]]; then
  echo "b1n430 integration prerequisite failed: postgres did not become ready" >&2
  exit 2
fi

docker exec -i "$postgres_name" psql \
  --set ON_ERROR_STOP=1 \
  --set "authenticator_password=$authenticator_password" \
  --username postgres \
  --dbname options \
  < "$repo_root/tests/integration/b1n430_postgrest_roles.sql" >/dev/null
docker exec -i "$postgres_name" psql \
  --set ON_ERROR_STOP=1 \
  --username postgres \
  --dbname options \
  < "$repo_root/tests/integration/b1n430_postgrest_schema.sql" >/dev/null
docker exec -i "$postgres_name" psql \
  --set ON_ERROR_STOP=1 \
  --username postgres \
  --dbname options \
  < "$repo_root/supabase/migrations/202608030002_b1n430_bounded_reads.sql" >/dev/null
docker exec -i "$postgres_name" psql \
  --set ON_ERROR_STOP=1 \
  --username postgres \
  --dbname options \
  < "$repo_root/tests/integration/b1n430_postgrest_fixture.sql" >/dev/null

docker run --detach --rm \
  --name "$postgrest_name" \
  --network "$network_name" \
  --publish 127.0.0.1::3000 \
  --env "PGRST_DB_URI=$database_uri" \
  --env "PGRST_DB_SCHEMAS=public" \
  --env "PGRST_DB_ANON_ROLE=anon" \
  --env "PGRST_JWT_SECRET=$jwt_secret" \
  --env "PGRST_DB_POOL=10" \
  "$postgrest_image" >/dev/null

postgrest_port="$(docker port "$postgrest_name" 3000/tcp | sed -n 's/.*://p' | head -1)"
if [[ -z "$postgrest_port" ]]; then
  echo "b1n430 integration prerequisite failed: could not resolve PostgREST port" >&2
  exit 2
fi
postgrest_url="http://127.0.0.1:$postgrest_port"

postgrest_ready=false
for _attempt in $(seq 1 60); do
  if curl --fail --silent --show-error "$postgrest_url/" >/dev/null 2>&1; then
    postgrest_ready=true
    break
  fi
  sleep 1
done
if [[ "$postgrest_ready" != true ]]; then
  echo "b1n430 integration prerequisite failed: PostgREST did not become ready" >&2
  docker logs "$postgrest_name" >&2 || true
  exit 2
fi

env -i \
  "PATH=$repo_root/.venv/bin:$(dirname "$uv_bin"):/usr/local/bin:/usr/bin:/bin" \
  "HOME=${HOME}" \
  "TMPDIR=$runtime_root" \
  "PYTHONPATH=$repo_root" \
  "PYTHONDONTWRITEBYTECODE=1" \
  "PYTHONHASHSEED=0" \
  "TZ=UTC" \
  "LC_ALL=C" \
  "APP_ENV=test" \
  "SUPABASE_URL=http://127.0.0.1:9" \
  "SUPABASE_ANON_KEY=local-integration-placeholder" \
  "SUPABASE_SERVICE_ROLE_KEY=local-integration-placeholder" \
  "POSITION_CURSOR_SECRET=$cursor_secret" \
  "B1N430_POSTGREST_URL=$postgrest_url" \
  "B1N430_ANON_TOKEN=$anon_token" \
  "B1N430_AUTHENTICATED_TOKEN=$authenticated_token" \
  "B1N430_SERVICE_TOKEN=$service_token" \
  "$uv_bin" run --frozen --offline pytest \
    -q -m integration \
    tests/integration/test_b1n430_bounded_reads_postgrest.py
