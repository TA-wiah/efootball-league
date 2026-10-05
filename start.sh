#!/bin/sh
# Start the league site: update the database, set up the admin login, then serve with gunicorn.
set -e
if [ -z "$DATABASE_URL" ]; then
  echo "NOTE: no DATABASE_URL, so the league is stored in a local SQLite file. On hosts whose disk is wiped on restart"
  echo "      (like free Koyeb), set DATABASE_URL to a Postgres database (e.g. free Neon) or you'll lose your data."
fi
if [ -z "$SECRET_KEY" ]; then
  echo "NOTE: no SECRET_KEY set. Set one on your host, or everyone is logged out whenever the disk is wiped."
fi
python manage.py migrate --noinput
python manage.py ensure_admin
exec gunicorn league_site.wsgi --bind "0.0.0.0:${PORT:-3000}" --workers "${WEB_CONCURRENCY:-2}" --threads 4 \
  --timeout 30 --graceful-timeout 10 --access-logfile - --forwarded-allow-ips "*"
