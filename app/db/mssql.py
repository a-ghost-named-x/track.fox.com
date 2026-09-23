"""MSSQL connection helper.

This connection is read-only, both by convention and by the SELECT-only login
it uses. Never add INSERT, UPDATE, DELETE or DDL statements here.
"""
from contextlib import contextmanager

import pyodbc

from app.config import settings


@contextmanager
def get_mssql_connection():
    conn = pyodbc.connect(settings.mssql_connection_string, timeout=5)
    try:
        yield conn
    finally:
        conn.close()


def run_readonly_query(query: str, params: tuple = ()) -> list[dict]:
    """Runs a SELECT and returns rows as a list of dicts.

    The SQL text isn't inspected; the read-only guarantee comes from the
    login's permissions.
    """
    with get_mssql_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
