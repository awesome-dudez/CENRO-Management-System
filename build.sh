#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

# Apply database migrations
python manage.py migrate --noinput

# Ensure an admin user exists on each deploy (username: admin, password: admin123)
python manage.py create_admin

# Collect static files for WhiteNoise
python manage.py collectstatic --noinput
