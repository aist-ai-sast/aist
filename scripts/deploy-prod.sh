#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-.env}"
DOMAIN="${DOMAIN:-aist.itsec-europe.com}"
DAST_CONNECTOR_IMAGE="${DAST_CONNECTOR_IMAGE:-aist-dast-connector:v2}"
DAST_CONNECTOR_DOCKERFILE="sast-combinator/sast-pipeline/Dockerfiles/dast_connector/Dockerfile"
DAST_CONNECTOR_CONTEXT="sast-combinator/sast-pipeline"

cd "${PROJECT_DIR}"

echo "== Building standalone DAST connector =="
docker build \
  --file "${DAST_CONNECTOR_DOCKERFILE}" \
  --target runtime \
  --tag "${DAST_CONNECTOR_IMAGE}" \
  "${DAST_CONNECTOR_CONTEXT}"

echo "== Starting AIST application services =="
docker compose --env-file "${COMPOSE_ENV_FILE}" up -d --build

echo "== Running containers =="
docker compose --env-file "${COMPOSE_ENV_FILE}" ps --status running

required_services=(nginx uwsgi postgres valkey celeryworker celerybeat context-extractor-mcp claude-bridge)
running_services="$(docker compose --env-file "${COMPOSE_ENV_FILE}" ps --status running --services)"
for service in "${required_services[@]}"; do
  if ! grep -qx "${service}" <<<"${running_services}"; then
    echo "Required service is not running: ${service}" >&2
    exit 1
  fi
done

echo "== Migration state =="
docker compose --env-file "${COMPOSE_ENV_FILE}" exec -T uwsgi \
  python3 manage.py migrate --check

echo "== Generic execution runtime checks =="
docker compose --env-file "${COMPOSE_ENV_FILE}" exec -T uwsgi \
  python3 manage.py check --deploy --tag aist_execution

echo "== Nginx config test =="
docker compose --env-file "${COMPOSE_ENV_FILE}" exec -T nginx nginx -t

echo "== TLS certificate in nginx container =="
docker compose --env-file "${COMPOSE_ENV_FILE}" exec -T nginx \
  openssl x509 -in /etc/nginx/ssl/nginx.crt -noout -issuer -subject -dates -ext subjectAltName

echo "== External TLS check for ${DOMAIN} =="
echo | openssl s_client -servername "${DOMAIN}" -connect "${DOMAIN}:443" 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates -ext subjectAltName
