#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
CLIENT_UI_DIR="$ROOT_DIR/client-ui"

NODE_IMAGE=${NODE_IMAGE:-node:20-bookworm}
PLAYWRIGHT_IMAGE=${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright:v1.54.2-jammy}

run_unit=1
run_e2e=1
clean=0
show_logs=0
base_url=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unit)
      run_unit=1
      run_e2e=0
      shift
      ;;
    --e2e)
      run_unit=0
      run_e2e=1
      shift
      ;;
    --all)
      run_unit=1
      run_e2e=1
      shift
      ;;
    --base-url)
      base_url="${2:-}"
      if [[ -z "$base_url" ]]; then
        echo "--base-url requires a value" >&2
        exit 1
      fi
      shift 2
      ;;
    --clean)
      clean=1
      shift
      ;;
    --logs)
      show_logs=1
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: ./run-client-ui-tests.zsh [--unit|--e2e|--all] [--base-url URL] [--clean] [--logs]

Default mode: --all (unit + e2e)

Modes:
  --unit      Run Client UI unit tests only (Docker).
  --e2e       Run Client UI e2e tests only (Docker + compose stack).
  --all       Run both unit and e2e tests.

Options:
  --base-url  Existing app URL for e2e (skip compose bootstrap).
  --clean     For e2e bootstrap mode: docker compose down -v before run.
  --logs      Print compose logs after e2e run.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$CLIENT_UI_DIR" ]]; then
  echo "client-ui directory not found: $CLIENT_UI_DIR" >&2
  exit 1
fi

cd "$ROOT_DIR"

if [[ ! -f "$ROOT_DIR/vendor/defectdojo/requirements.txt" || ! -f "$ROOT_DIR/vendor/defectdojo/requirements-dev.txt" ]]; then
  echo "Missing vendor/defectdojo submodule content. Run: git submodule update --init --recursive" >&2
  exit 1
fi

if [[ ! -d "$ROOT_DIR/sast-combinator/sast-pipeline" ]]; then
  echo "Missing sast-combinator submodule content. Run: git submodule update --init --recursive" >&2
  exit 1
fi

compose_project_name=${COMPOSE_PROJECT_NAME:-aist_client_ui}
compose_network="${compose_project_name}_default"
compose_args=( -p "$compose_project_name" -f docker-compose.yml -f docker-compose.integration.yml )

run_unit_tests() {
  echo "==> Client UI unit tests (Docker: ${NODE_IMAGE})"
  docker run --rm \
    -v "$CLIENT_UI_DIR:/workspace/client-ui" \
    -w /workspace/client-ui \
    "$NODE_IMAGE" \
    bash -lc '
      set -euo pipefail
      npm install --no-audit --no-fund
      npm run test
    '
}

run_e2e_tests() {
  echo "==> Client UI e2e tests"

  local started_stack=0

  cleanup_stack() {
    if [[ $started_stack -eq 1 ]]; then
      if [[ $show_logs -eq 1 ]]; then
        docker compose "${compose_args[@]}" logs --tail=2500 || true
      fi
      docker compose "${compose_args[@]}" down --remove-orphans || true
    fi
  }
  trap cleanup_stack EXIT

  export DJANGO_VERSION=${DJANGO_VERSION:-debian}
  export NGINX_VERSION=${NGINX_VERSION:-alpine}
  export DD_DATABASE_USER=${DD_DATABASE_USER:-aist}
  export DD_DATABASE_PASSWORD=${DD_DATABASE_PASSWORD:-aist}
  export DD_TEST_DATABASE_NAME=${DD_TEST_DATABASE_NAME:-test_aist}
  export DD_TEST_DATABASE_URL=${DD_TEST_DATABASE_URL:-postgresql://$DD_DATABASE_USER:$DD_DATABASE_PASSWORD@postgres:5432/$DD_TEST_DATABASE_NAME}
  export DD_HTTP_PORT=${DD_HTTP_PORT:-8080}
  export DD_TLS_PORT=${DD_TLS_PORT:-8443}
  export GENERATE_TLS_CERTIFICATE=${GENERATE_TLS_CERTIFICATE:-false}
  export USE_TLS=${USE_TLS:-false}
  export DD_ALLOWED_HOSTS=${DD_ALLOWED_HOSTS:-localhost,127.0.0.1,host.docker.internal,nginx,[::1]}
  export DD_CSRF_TRUSTED_ORIGINS=${DD_CSRF_TRUSTED_ORIGINS:-[\"http://127.0.0.1:${DD_HTTP_PORT}\",\"http://host.docker.internal:${DD_HTTP_PORT}\",\"http://nginx:8080\"]}
  export DD_SESSION_COOKIE_SECURE=${DD_SESSION_COOKIE_SECURE:-False}
  export DD_CSRF_COOKIE_SECURE=${DD_CSRF_COOKIE_SECURE:-False}
  export DD_AIST_AUTH_LOGIN_THROTTLE_RATE=${DD_AIST_AUTH_LOGIN_THROTTLE_RATE:-200/min}
  export DD_ADMIN_USER=${DD_ADMIN_USER:-admin}
  export DD_ADMIN_PASSWORD=${DD_ADMIN_PASSWORD:-AdminsLoveIntegrationtests!}
  export DD_EMAIL_URL=${DD_EMAIL_URL:-smtp://user:password@localhost:25}
  export AIST_TMP_DIR=${AIST_TMP_DIR:-/tmp/aist}
  export ACME_WEBROOT_HOST=${ACME_WEBROOT_HOST:-$AIST_TMP_DIR/acme-webroot}
  export NGINX_SSL_HOST_DIR=${NGINX_SSL_HOST_DIR:-$AIST_TMP_DIR/nginx-ssl}

  mkdir -p "$ACME_WEBROOT_HOST" "$NGINX_SSL_HOST_DIR"

  if [[ -z "${FIELD_ENCRYPTION_KEY:-}" ]]; then
    export FIELD_ENCRYPTION_KEY=$(docker run --rm python:3.12-alpine \
      python -c 'import os, base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')
  fi

  if [[ -z "$base_url" ]]; then
    if [[ $clean -eq 1 ]]; then
      docker compose "${compose_args[@]}" down -v --remove-orphans
    fi

    echo "==> Building app images"
    docker compose "${compose_args[@]}" build uwsgi nginx

    echo "==> Starting db/cache"
    started_stack=1
    docker compose "${compose_args[@]}" up --no-deps -d postgres valkey

    echo "==> Initializer"
    docker compose "${compose_args[@]}" up --no-deps --exit-code-from initializer initializer

    echo "==> Seeding demo access/business data"
    docker compose "${compose_args[@]}" run --rm --no-deps --entrypoint /bin/bash uwsgi -lc \
      "cd /app && python3 manage.py bootstrap_demo_access --password '${DD_ADMIN_PASSWORD}'"

    echo "==> Starting uwsgi/nginx"
    docker compose "${compose_args[@]}" up --no-deps -d uwsgi nginx
    started_stack=1

    local readiness_url="http://127.0.0.1:${DD_HTTP_PORT}/"
    echo "==> Waiting for app readiness at ${readiness_url}"
    local i=0
    until curl -fsS "$readiness_url" >/dev/null; do
      i=$((i + 1))
      if [[ $i -ge 90 ]]; then
        echo "Timed out waiting for ${readiness_url}" >&2
        exit 1
      fi
      sleep 2
    done

    # Playwright runs in a container on the same compose network.
    base_url="http://nginx:8080"
  fi

  echo "==> Running Playwright in Docker: ${PLAYWRIGHT_IMAGE}"
  local docker_network_args=()
  if [[ $started_stack -eq 1 ]]; then
    docker_network_args=(--network "$compose_network")
  fi

  docker run --rm \
    "${docker_network_args[@]}" \
    -v "$CLIENT_UI_DIR:/workspace/client-ui" \
    -w /workspace/client-ui \
    -e PLAYWRIGHT_BASE_URL="$base_url" \
    -e PLAYWRIGHT_USERNAME="$DD_ADMIN_USER" \
    -e PLAYWRIGHT_PASSWORD="$DD_ADMIN_PASSWORD" \
    "$PLAYWRIGHT_IMAGE" \
    bash -lc '
      set -euo pipefail
      npm install --no-audit --no-fund
      npx playwright install chromium
      npm run test:e2e
    '

  trap - EXIT
  cleanup_stack
}

if [[ $run_unit -eq 1 ]]; then
  run_unit_tests
fi

if [[ $run_e2e -eq 1 ]]; then
  run_e2e_tests
fi
