#!/bin/bash
umask 0002

id

set -e  # needed to handle "exit" correctly
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-aist_site.settings}

. /secret-file-loader.sh
. /reach_database.sh
. /reach_broker.sh

# Allow for bind-mount multiple settings.py overrides
shopt -s nullglob
FILES=(/app/docker/extra_settings/*.py)
shopt -u nullglob
if [ "${#FILES[@]}" -gt 0 ]; then
    COMMA_LIST=$(printf "%s\n" "${FILES[@]}" | paste -sd ", " -)
    echo "============================================================"
    echo "     Overriding DefectDojo's local_settings.py with multiple"
    echo "     Files: $COMMA_LIST"
    echo "============================================================"
    cp "${FILES[@]}" /app/vendor/defectdojo/dojo/settings/
fi

wait_for_database_to_be_reachable
wait_for_broker_to_be_reachable
echo

if [ "${DD_CELERY_WORKER_POOL_TYPE}" = "prefork" ]; then
  EXTRA_PARAMS=("--autoscale=${DD_CELERY_WORKER_AUTOSCALE_MAX},${DD_CELERY_WORKER_AUTOSCALE_MIN}"
    "--prefetch-multiplier=${DD_CELERY_WORKER_PREFETCH_MULTIPLIER}")
else
  EXTRA_PARAMS=()
fi

# do the check with Django stack
python3 manage.py check

exec celery --app=dojo \
    worker \
  --loglevel="${DD_CELERY_LOG_LEVEL}" \
  --pool="${DD_CELERY_WORKER_POOL_TYPE}" \
  --concurrency="${DD_CELERY_WORKER_CONCURRENCY:-1}" \
  "${EXTRA_PARAMS[@]}"
