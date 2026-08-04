#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
run_suffix="$$"
network_name="b1n423-network-${run_suffix}"
postgres_name="b1n423-postgres-${run_suffix}"
postgrest_name="b1n423-postgrest-${run_suffix}"
network_id=""
postgres_id=""
postgrest_id=""

postgres_image="postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
postgrest_image="postgrest/postgrest:v12.2.12@sha256:5f4ce744539bbba786b4e24dbbd95bdb2a956dcf568c5374995a0ff4a68f5bd2"

cleanup() {
    if [[ -n "${postgrest_id}" ]]; then
        docker rm --force "${postgrest_id}" >/dev/null 2>&1 || true
    fi
    if [[ -n "${postgres_id}" ]]; then
        docker rm --force "${postgres_id}" >/dev/null 2>&1 || true
    fi
    if [[ -n "${network_id}" ]]; then
        docker network rm "${network_id}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

for required_command in docker openssl curl sed uv; do
    if ! command -v "${required_command}" >/dev/null 2>&1; then
        echo "Missing required command: ${required_command}" >&2
        exit 1
    fi
done

if ! docker info >/dev/null 2>&1; then
    echo "Docker is required; start its local daemon and retry." >&2
    exit 1
fi

network_id="$(docker network create "${network_name}")"
postgres_id="$(docker run --detach \
    --name "${postgres_name}" \
    --network "${network_name}" \
    --network-alias b1n423-db \
    --env POSTGRES_DB=postgres \
    --env POSTGRES_HOST_AUTH_METHOD=trust \
    --tmpfs /var/lib/postgresql/data:size=512m \
    "${postgres_image}")"

postgres_ready=0
for _ in $(seq 1 30); do
    if docker exec "${postgres_name}" pg_isready --username postgres --dbname postgres \
        >/dev/null 2>&1; then
        postgres_ready=1
        break
    fi
    sleep 1
done
if [[ "${postgres_ready}" != "1" ]]; then
    docker logs "${postgres_name}" >&2
    exit 1
fi

psql=(docker exec --interactive "${postgres_name}" psql \
    --username postgres --dbname postgres --set ON_ERROR_STOP=1)

"${psql[@]}" < "${repo_root}/tests/integration/b1n423_postgrest_roles.sql"

# Apply the canonical table definitions directly from src/db/schema.sql so the
# integration fixture cannot silently drift from the repository schema.
sed -n '/^create table if not exists order_events (/,/^);/p' \
    "${repo_root}/src/db/schema.sql" | "${psql[@]}"
sed -n '/^create table if not exists mm_quotes (/,/^);/p' \
    "${repo_root}/src/db/schema.sql" | "${psql[@]}"

"${psql[@]}" < "${repo_root}/tests/integration/b1n423_postgrest_grants.sql"
"${psql[@]}" < \
    "${repo_root}/supabase/migrations/202608030001_b1n423_mm_exposure.sql"

jwt_signing_value="$(openssl rand -hex 32)"
postgrest_id="$(docker run --detach \
    --name "${postgrest_name}" \
    --network "${network_name}" \
    --publish 127.0.0.1::3000 \
    --env PGRST_DB_URI=postgres://authenticator@b1n423-db:5432/postgres \
    --env PGRST_DB_SCHEMAS=public \
    --env PGRST_DB_ANON_ROLE=anon \
    --env PGRST_JWT_SECRET="${jwt_signing_value}" \
    "${postgrest_image}")"

port_mapping="$(docker port "${postgrest_name}" 3000/tcp)"
postgrest_url="http://127.0.0.1:${port_mapping##*:}"

postgrest_ready=0
for _ in $(seq 1 30); do
    if curl --fail --silent --show-error "${postgrest_url}/" >/dev/null 2>&1; then
        postgrest_ready=1
        break
    fi
    sleep 1
done
if [[ "${postgrest_ready}" != "1" ]]; then
    docker logs "${postgrest_name}" >&2
    exit 1
fi

cd "${repo_root}"
B1N423_POSTGREST_URL="${postgrest_url}" \
B1N423_JWT_SIGNING_VALUE="${jwt_signing_value}" \
SUPABASE_URL="${postgrest_url}" \
SUPABASE_ANON_KEY=local-integration-placeholder \
SUPABASE_SERVICE_ROLE_KEY=local-integration-placeholder \
    uv run --frozen pytest -q tests/integration/test_b1n423_mm_exposure_postgrest.py
