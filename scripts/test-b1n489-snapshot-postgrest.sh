#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="${HARNESS_REPO_ROOT:-$(cd "$script_dir/.." && pwd -P)}"
python_bin="$repo_root/.venv/bin/python"
uv_bin="$(command -v uv)"
postgres_image="postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
postgrest_image="postgrest/postgrest:v12.2.12@sha256:5f4ce744539bbba786b4e24dbbd95bdb2a956dcf568c5374995a0ff4a68f5bd2"
suffix="$$"
network="b1n489-network-$suffix"
postgres="b1n489-postgres-$suffix"
postgrest="b1n489-postgrest-$suffix"
cleanup(){ docker rm -f "$postgrest" "$postgres" >/dev/null 2>&1 || true; docker network rm "$network" >/dev/null 2>&1 || true; }
trap cleanup EXIT

password="$($python_bin -c 'import secrets; print(secrets.token_hex(18))')"
auth_password="$($python_bin -c 'import secrets; print(secrets.token_hex(18))')"
jwt_secret="$($python_bin -c 'import secrets; print(secrets.token_hex(32))')"
make_token(){ "$python_bin" - "$jwt_secret" "$1" <<'PY'
import base64, hashlib, hmac, json, sys, time
def enc(v): return base64.urlsafe_b64encode(json.dumps(v,separators=(",",":")).encode()).decode().rstrip("=")
secret,role=sys.argv[1:]; h=enc({"alg":"HS256","typ":"JWT"}); p=enc({"role":role,"exp":int(time.time())+3600}); s=base64.urlsafe_b64encode(hmac.new(secret.encode(),f"{h}.{p}".encode(),hashlib.sha256).digest()).decode().rstrip("="); print(f"{h}.{p}.{s}")
PY
}
anon_token="$(make_token anon)"; service_token="$(make_token service_role)"
database_uri="postgresql://authenticator"
database_uri+=":${auth_password}@postgres:5432/options"

docker network create "$network" >/dev/null
docker run -d --rm --name "$postgres" --network "$network" --network-alias postgres -e POSTGRES_DB=options -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$password" "$postgres_image" >/dev/null
sleep 3
printf "CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN; CREATE ROLE service_role NOLOGIN BYPASSRLS; CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD '%s'; GRANT anon, authenticated, service_role TO authenticator; GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;\n" "$auth_password" | docker exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d options >/dev/null
docker exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d options < "$repo_root/src/db/schema.sql" >/dev/null
for migration in $(find "$repo_root/supabase/migrations" -maxdepth 1 -name '*.sql' | sort); do
  if [[ "$migration" == *202608150002* ]]; then
    printf 'GRANT USAGE ON SCHEMA public, private TO service_role; GRANT ALL ON ALL TABLES IN SCHEMA public, private TO service_role; GRANT ALL ON ALL SEQUENCES IN SCHEMA public, private TO service_role; GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public, private TO service_role;\n' | docker exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d options >/dev/null
  fi
  docker exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d options < "$migration" >/dev/null
done
docker exec -i "$postgres" psql -v ON_ERROR_STOP=1 -U postgres -d options < "$repo_root/tests/integration/b1n489_fixture.sql" >/dev/null

docker run -d --rm --name "$postgrest" --network "$network" -p 127.0.0.1::3000 -e "PGRST_DB_URI=$database_uri" -e PGRST_DB_SCHEMAS=public -e PGRST_DB_ANON_ROLE=anon -e PGRST_JWT_SECRET="$jwt_secret" "$postgrest_image" >/dev/null
port="$(docker port "$postgrest" 3000/tcp | sed -n 's/.*://p' | head -1)"
url="http://127.0.0.1:$port"
for _ in $(seq 1 60); do curl -fsS "$url/" >/dev/null 2>&1 && break; sleep 1; done

env -i PATH="$repo_root/.venv/bin:$(dirname "$uv_bin"):/usr/local/bin:/usr/bin:/bin" HOME="$HOME" PYTHONPATH="$repo_root" PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 TZ=UTC LC_ALL=C SUPABASE_URL=http://127.0.0.1:9 SUPABASE_ANON_KEY=local-integration-placeholder SUPABASE_SERVICE_ROLE_KEY=local-integration-placeholder B1N489_POSTGREST_URL="$url" B1N489_SERVICE_TOKEN="$service_token" B1N489_ANON_TOKEN="$anon_token" "$uv_bin" run --frozen --offline pytest -q -m integration tests/integration/test_b1n489_snapshot_postgrest.py
