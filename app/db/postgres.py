"""Postgres connection helper (manual-entry data — owned by this app)."""
from contextlib import contextmanager

import psycopg

from app.config import settings


@contextmanager
def get_pg_connection():
    """Yields a psycopg connection, closed automatically on exit.

    Uses a dedicated low-privilege role (set via POSTGRES_USER), never the
    superuser — grants should be scoped to exactly the tables this app needs
    (entries, standards). See sql/schema.sql for table definitions and set up
    the role/grants separately (not handled by this scaffold).
    """
    conn = psycopg.connect(settings.postgres_dsn)
    try:
        yield conn
    finally:
        conn.close()
