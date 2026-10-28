#!/bin/bash
# Create PostgreSQL roles + database for the multi-user app and apply the schema.
# Idempotent. Run as root (WSL or the cloud VM):
#   bash deploy/postgres_setup.sh /path/to/app [python]
# Passwords are generated once and appended to <app>/.env (never printed).
set -euo pipefail
APP=${1:?usage: postgres_setup.sh APP_DIR [PYTHON]}
PY=${2:-$APP/venv/bin/python}
ENV_FILE="$APP/.env"
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

get_or_create() {   # $1 = env var name
  local v
  v=$(grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)
  if [ -z "$v" ]; then
    v=$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | head -c 32)
    printf '\n%s=%s\n' "$1" "$v" >> "$ENV_FILE"
  fi
  printf '%s' "$v"
}
grep -q '^PG_DATABASE=' "$ENV_FILE" || printf '\n# PostgreSQL (multi-user data with row-level security)\nPG_HOST=127.0.0.1\nPG_PORT=5432\nPG_DATABASE=nse_app\n' >> "$ENV_FILE"
OWNER_PW=$(get_or_create PG_OWNER_PASSWORD)
APP_PW=$(get_or_create PG_APP_PASSWORD)
SVC_PW=$(get_or_create PG_SERVICE_PASSWORD)
chmod 600 "$ENV_FILE" 2>/dev/null || true

psql_su() { sudo -u postgres psql -v ON_ERROR_STOP=1 -qtA "$@"; }
psql_su -v owner_pw="$OWNER_PW" -v app_pw="$APP_PW" -v svc_pw="$SVC_PW" <<'SQL'
SELECT 'CREATE ROLE nse_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nse_owner') \gexec
SELECT 'CREATE ROLE nse_app LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nse_app') \gexec
SELECT 'CREATE ROLE nse_service LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nse_service') \gexec
ALTER ROLE nse_owner PASSWORD :'owner_pw' CREATEDB NOSUPERUSER NOBYPASSRLS;
ALTER ROLE nse_app PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOBYPASSRLS;
ALTER ROLE nse_service PASSWORD :'svc_pw' NOSUPERUSER NOCREATEDB BYPASSRLS;
SQL
# nse_app = real data; nse_app_test = throw-away database used by the automated tests
for DB in nse_app nse_app_test; do
  psql_su -v db="$DB" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER nse_owner', :'db') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO nse_app, nse_service', :'db') \gexec
SQL
  psql_su -d "$DB" -c "ALTER SCHEMA public OWNER TO nse_owner"
done

cd "$APP"
"$PY" -m alert_system db-migrate
PG_DATABASE=nse_app_test "$PY" -m alert_system db-migrate
echo "PostgreSQL ready: database nse_app, roles nse_owner / nse_app (RLS) / nse_service (jobs)."
