#!/usr/bin/env bash
# Creates the dedicated, low-privilege Postgres role the app connects as.
# Runs automatically by the official postgres image's init mechanism
# (any executable .sh in docker-entrypoint-initdb.d/ is sourced on first
# container start, before the volume has existing data).
#
# Reads APP_DB_USER / APP_DB_PASSWORD from the environment (passed through
# from .env via docker-compose.yml) rather than hardcoding credentials in a
# .sql file that gets committed to git.
#
# Reference only: kept for the record of what was run to bootstrap the
# manual-entry database. Postgres now lives on a dedicated standalone server
# instead of a bundled `db` container, so this script isn't executed by
# anything anymore — see docs/postgres-server-setup.md Step 4 for the manual
# equivalent that was actually run.
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
