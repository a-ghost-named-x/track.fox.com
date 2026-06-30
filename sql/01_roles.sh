#!/usr/bin/env bash
# Creates the dedicated, low-privilege Postgres role the app connects as.
# Runs automatically by the official postgres image's init mechanism
# (any executable .sh in docker-entrypoint-initdb.d/ is sourced on first
# container start, before the volume has existing data).
#
# Reads APP_DB_USER / APP_DB_PASSWORD from the environment (passed through
# from .env via docker-compose.yml) rather than hardcoding credentials in a
# .sql file that gets committed to git.
set -euo pipefail

: "${APP_DB_USER:?APP_DB_USER must be set}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '${APP_DB_USER}') THEN
            CREATE ROLE ${APP_DB_USER} WITH LOGIN PASSWORD '${APP_DB_PASSWORD}';
        END IF;
    END
    \$\$;
EOSQL
