#!/bin/bash

set -e  # needed to handle "exit" correctly
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-aist_site.settings}

. /secret-file-loader.sh
. /reach_database.sh

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

umask 0002

wait_for_database_to_be_reachable
python manage.py complete_initialization

exec "$@"
