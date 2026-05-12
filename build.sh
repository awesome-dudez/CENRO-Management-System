#!/usr/bin/env bash
# Keep the build free of DB connections: Render build workers often cannot resolve
# private Postgres hostnames (migrate/create_default_admin belong in start.sh).
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
