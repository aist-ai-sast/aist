#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)

export COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml:${ROOT_DIR}/docker-compose.override.aist_cd.yml:${ROOT_DIR}/docker-compose.override.unit_tests_cicd.yml"
export DEFECT_DOJO_OS=${DEFECT_DOJO_OS:-debian}
export DJANGO_VERSION=${DJANGO_VERSION:-debian}
export DD_DATABASE_USER=defectdojo
export DD_DATABASE_PASSWORD=defectdojo
export DD_DATABASE_NAME=test_defectdojo
export DD_TEST_DATABASE_NAME=test_defectdojo
export DD_APPEND_SLASH=False

clean=0
show_logs=0
full_run=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean)
      clean=1
      shift
      ;;
    --logs)
      show_logs=1
      shift
      ;;
    --full)
      full_run=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./run-rest-framework-tests.zsh [--clean] [--logs] [--full]

Options:
  --clean  Remove containers/volumes before test run.
  --logs   Print uwsgi logs after test run.
  --full   Run both DD and AIST suites as in CI workflow.
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "${FORCE_AMD64:-0}" == "1" && "$(uname -m)" == "arm64" ]]; then
  export DOCKER_DEFAULT_PLATFORM=${DOCKER_DEFAULT_PLATFORM:-linux/amd64}
fi

cd "$ROOT_DIR"

if [[ $clean -eq 1 ]]; then
  docker compose down -v
fi

docker compose build uwsgi

if [[ $full_run -eq 1 ]]; then
  docker compose up --no-deps -d postgres webhook.endpoint
  docker compose up --no-deps --exit-code-from uwsgi uwsgi
  docker compose down

  docker compose up --no-deps -d postgres webhook.endpoint
  docker compose run --rm --no-deps --entrypoint /entrypoint-unit-tests-aist.sh uwsgi
else
  docker compose up --no-deps -d postgres webhook.endpoint
  docker compose run --no-deps --rm --entrypoint /bin/bash uwsgi -lc '
set -e
. /secret-file-loader.sh
. /reach_database.sh

cd /app
export DD_APPEND_SLASH=False
unset DD_DATABASE_URL
unset DD_CELERY_BROKER_URL

wait_for_database_to_be_reachable

python3 manage.py spectacular --fail-on-warn > /dev/null
python3 manage.py makemigrations --no-input --check --dry-run --verbosity 3
python3 manage.py migrate

python3 manage.py test aist.test -v 3 --keepdb --no-input
'
fi

if [[ $show_logs -eq 1 ]]; then
  docker compose logs --tail=2500 uwsgi
fi

docker compose down
