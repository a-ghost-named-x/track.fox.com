"""Postgres connection helper."""
from contextlib import contextmanager

import psycopg

from app.config import settings


@contextmanager
def get_pg_connection():
    """Yields a psycopg connection, closed automatically on exit.

    Connects as the app's low-privilege role (APP_DB_USER / APP_DB_PASSWORD).
    Parameters are passed as keywords rather than a DSN string so a password
    containing URI-reserved characters (/, #, ?) doesn't break parsing.
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
