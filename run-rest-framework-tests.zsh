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

if [[ "${FORCE_AMD64:-0}" == "1" && "$(uname -m)" == "arm64" ]]; then
  export DOCKER_DEFAULT_PLATFORM=${DOCKER_DEFAULT_PLATFORM:-linux/amd64}
fi

cd "$ROOT_DIR"

if [[ ${1:-} == "--clean" ]]; then
  docker compose down -v
fi

docker compose build uwsgi

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

python3 manage.py test dojo.aist.test -v 3 --keepdb --no-input
'

if [[ ${1:-} == "--logs" ]]; then
  docker compose logs --tail=2500 uwsgi
fi

docker compose down
