#!/bin/bash
# Run AIST unit tests with a setup for CI/CD.
set -e
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-aist_site.settings}

. /secret-file-loader.sh
. /reach_database.sh

cd /app || exit

# Allow for bind-mount multiple settings.py overrides.
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

# Unset URLs so discrete DD_* settings are honored.
unset DD_DATABASE_URL
unset DD_CELERY_BROKER_URL

wait_for_database_to_be_reachable

python3 manage.py makemigrations --no-input --check --dry-run --verbosity 3 || {
    cat <<-EOF

********************************************************************************

You made changes to the models without creating a DB migration for them.

**NEVER** change existing migrations, create a new one.

If you're not familiar with migrations in Django, please read the
great documentation thoroughly:
https://docs.djangoproject.com/en/5.0/topics/migrations/

********************************************************************************

EOF
    exit 1
}

python3 manage.py migrate

echo "AIST Unit Tests"
echo "------------------------------------------------------------"
python3 manage.py test aist.test --keepdb --no-input || {
    exit 1
}
