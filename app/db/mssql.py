"""MSSQL connection helper.

IMPORTANT: this connection is READ-ONLY by convention and by the credential
used (a low-privilege SELECT-only login, per the architecture decision to
never burden or write to the production MSSQL server). Do not add INSERT,
UPDATE, DELETE, or DDL statements anywhere that uses this connection.
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

    Caller is responsible for ensuring `query` is a SELECT only — this
    function does not validate the SQL text itself, since the read-only
    guarantee comes from the database credential's permissions, not from
    string inspection here.
    """
    with get_mssql_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
