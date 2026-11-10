#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-.env}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
DAST_CONNECTOR_DOCKERFILE="sast-combinator/sast-pipeline/Dockerfiles/dast_connector/Dockerfile"
DAST_CONNECTOR_CONTEXT="sast-combinator/sast-pipeline"

cd "${PROJECT_DIR}"

deployment_cli=(python3 "$PROJECT_DIR/scripts/deployment_config.py" --project-dir "$PROJECT_DIR"
  --env-file "$COMPOSE_ENV_FILE" --compose-file "$COMPOSE_FILE")
compose=("${deployment_cli[@]}" --compose)

# Validate before building or starting anything. Only operational values are returned.
deployment=$("${deployment_cli[@]}")
ORIGIN=$(printf '%s' "$deployment" | python3 -c 'import json,sys; print(json.load(sys.stdin)["origin"])')
TLS_DIR=$(printf '%s' "$deployment" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tls_dir"])')
if [[ ! -s "$TLS_DIR/nginx.crt" || ! -s "$TLS_DIR/nginx.key" ]]; then
  echo "Provision the persistent TLS certificate before starting AIST" >&2
  exit 1
fi

echo "== Building application images =="
"${compose[@]}" build
# Read the connector tag from the same catalog the execution runtime uses.
DAST_CONNECTOR_IMAGE=$("${compose[@]}" run --rm --no-deps --entrypoint python3 uwsgi -c '
from pathlib import Path
import yaml
catalog = yaml.safe_load(Path("/app/sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml").read_text())
print(next(entry["image"] for entry in catalog["analyzers"] if entry["name"] == "dast"))
')

echo "== Building standalone DAST connector =="
docker build \
  --file "${DAST_CONNECTOR_DOCKERFILE}" \
  --target runtime \
  --tag "${DAST_CONNECTOR_IMAGE}" \
  "${DAST_CONNECTOR_CONTEXT}"

echo "== Starting AIST application services =="
# Apply migrations while schedulers and writers are stopped. A restored database must be present
# before this command on a migration target; the prepare phase never invokes this script.
"${compose[@]}" stop nginx celerybeat celeryworker claude-bridge uwsgi
"${compose[@]}" up -d postgres valkey
"${compose[@]}" run --rm --no-deps initializer
"${compose[@]}" up -d --no-build

echo "== Running containers =="
"${compose[@]}" ps --status running

required_services=(nginx uwsgi postgres valkey celeryworker celerybeat claude-bridge)
running_services="$("${compose[@]}" ps --status running --services)"
for service in "${required_services[@]}"; do
  if ! grep -qx "${service}" <<<"${running_services}"; then
    echo "Required service is not running: ${service}" >&2
    exit 1
  fi
done

echo "== Migration state =="
"${compose[@]}" exec -T uwsgi \
  python3 manage.py migrate --check

echo "== Generic execution runtime checks =="
"${compose[@]}" exec -T uwsgi \
  python3 manage.py check --deploy --tag aist_execution

echo "== Nginx config test =="
"${compose[@]}" exec -T nginx nginx -t

echo "== TLS certificate in nginx container =="
"${compose[@]}" exec -T nginx \
  openssl x509 -in /etc/nginx/ssl/nginx.crt -noout -issuer -subject -dates -ext subjectAltName

echo "== HTTPS readiness with certificate verification =="
curl --fail --silent --show-error --retry 12 --retry-delay 5 --retry-connrefused \
  --cacert "$TLS_DIR/nginx.crt" --output /dev/null "$ORIGIN/"
