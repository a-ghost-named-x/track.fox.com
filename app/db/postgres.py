"""Postgres connection helper (manual-entry data — owned by this app)."""
from contextlib import contextmanager

import psycopg

from app.config import settings


@contextmanager
def get_pg_connection():
    """Yields a psycopg connection, closed automatically on exit.

    Uses a dedicated low-privilege role (APP_DB_USER/APP_DB_PASSWORD in
    .env), never the superuser — grants should be scoped to exactly the
    tables this app needs (entries, standards). See sql/02_schema.sql for
    table definitions and grants.

    Connects with individual keyword arguments rather than a single DSN
    string — building a "postgresql://user:password@host/db" string via
    plain interpolation breaks if the password contains a URI-reserved
    character (/, #, ?, etc.), since that gets misread as part of the URI
    structure instead of the password. Passing params as kwargs sidesteps
    URI parsing entirely, so any password value is safe.
    """
    conn = psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
    )
    try:
        yield conn
    finally:
        conn.close()
