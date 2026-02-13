#!/bin/bash
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

umask 0002

# do the check with Django stack
python3 manage.py check

DD_UWSGI_LOGFORMAT_DEFAULT='[pid: %(pid)|app: -|req: -/-] %(addr) (%(dd_user)) {%(vars) vars in %(pktsize) bytes} [%(ctime)] %(method) %(uri) => generated %(rsize) bytes in %(msecs) msecs (%(proto) %(status)) %(headers) headers in %(hsize) bytes (%(switches) switches on core %(core))'

DD_UWSGI_EXTRA_ARGS="${DD_UWSGI_EXTRA_ARGS:-}"
if [ -n "${DD_UWSGI_MAX_FD}" ]; then
    DD_UWSGI_EXTRA_ARGS="${DD_UWSGI_EXTRA_ARGS} --max-fd ${DD_UWSGI_MAX_FD}"
fi

exec uwsgi \
  "--${DD_UWSGI_MODE}" "${DD_UWSGI_ENDPOINT}" \
  --protocol uwsgi \
  --enable-threads \
  --processes "${DD_UWSGI_NUM_OF_PROCESSES:-4}" \
  --threads "${DD_UWSGI_NUM_OF_THREADS:-4}" \
  --wsgi aist_site.wsgi:application \
  --buffer-size="${DD_UWSGI_BUFFER_SIZE:-8192}" \
  --http 0.0.0.0:8081 --http-to "${DD_UWSGI_ENDPOINT}" \
  --logformat "${DD_UWSGI_LOGFORMAT:-$DD_UWSGI_LOGFORMAT_DEFAULT}" \
  $DD_UWSGI_EXTRA_ARGS
  # HTTP endpoint is enabled for Kubernetes liveness checks. It should not be exposed as a service.
