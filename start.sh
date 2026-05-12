#!/usr/bin/env bash
# Render / production: run DB work at runtime (build containers cannot reach private Postgres DNS).
set -o errexit

python manage.py migrate --noinput
python manage.py create_default_admin

exec gunicorn cenro_mgmt.wsgi:application --bind "0.0.0.0:${PORT:-8000}"
