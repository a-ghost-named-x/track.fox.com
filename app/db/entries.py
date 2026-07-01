"""Data access and business logic for the manual-entry dashboard.

Houses the status-computation rule: an entry's status is derived from
comparing units_produced against that machine+time_slot's standard, at
write-time, and stored on the row (per the decision to snapshot status
rather than recompute it on every dashboard read).
"""
from datetime import date as date_type

from app.db.postgres import get_pg_connection
from app.models import Entry, EntryCreate


class StandardNotFoundError(Exception):
    """Raised when no standard exists for a given machine_id + time_slot.

    This is a real "we can't compute status" situation — it should surface
    as a clear error to whoever is using /console, not silently default to
    a guessed status.
    """


def get_standard(machine_id: str, time_slot: str) -> int:
    with get_pg_connection() as conn:
        row = conn.execute(
            "SELECT standard_units FROM standards WHERE machine_id = %s AND time_slot = %s",
            (machine_id, time_slot),
        ).fetchone()
    if row is None:
        raise StandardNotFoundError(
            f"No standard defined for machine_id={machine_id!r} time_slot={time_slot!r}. "
            "Add it to the standards table (see sql/seed_standards.sql) before logging entries for this slot."
        )
    return row[0]


def compute_status(units_produced: int, standard_units: int) -> str:
    return ":)" if units_produced >= standard_units else ":("


def create_entry(payload: EntryCreate) -> Entry:
    standard = get_standard(payload.machine_id, payload.time_slot)
    status = compute_status(payload.units_produced, standard)

    with get_pg_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO entries
                (machine_id, operator, time_slot, units_produced, status, issue, entered_by, entry_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.operator,
                payload.time_slot,
                payload.units_produced,
                status,
                payload.issue,
                payload.entered_by,
                payload.entry_date,
            ),
        ).fetchone()
        conn.commit()

    return Entry(id=row[0], status=status, **payload.model_dump())


def get_shift_activity(entry_date: date_type, active_slots: list[str]) -> dict[str, dict]:
    """Returns, per machine, the operator and issue currently in effect for
    the shift in progress (i.e. whichever of `active_slots` have entries so
    far today).

    - operator: from that machine's most recently created entry anywhere in
      the shift (whichever slot it was logged against) — "who's on it right
      now", shown once per machine row rather than repeated per cell.
    - issue: from that machine's most recently created entry in the shift
      that actually reported a non-empty issue. Once reported, it carries
      forward across the rest of the shift's slot cells (per the floor's
      request — e.g. "defective material" logged at 8AM should still show
      through 2PM) even if later slots in the shift are logged without
      repeating it. A newer issue replaces an older one ("newest wins");
      there's currently no way to explicitly clear an issue mid-shift short
      of the shift changing over.
    """
    with get_pg_connection() as conn:
        operator_rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id) machine_id, operator
            FROM entries
            WHERE entry_date = %s AND time_slot = ANY(%s)
            ORDER BY machine_id, created_at DESC
            """,
            (entry_date, active_slots),
        ).fetchall()

        issue_rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id) machine_id, issue
            FROM entries
            WHERE entry_date = %s AND time_slot = ANY(%s)
              AND issue IS NOT NULL AND issue <> ''
            ORDER BY machine_id, created_at DESC
            """,
            (entry_date, active_slots),
        ).fetchall()

    activity: dict[str, dict] = {
        machine_id: {"operator": operator, "issue": None}
        for machine_id, operator in operator_rows
    }
    for machine_id, issue in issue_rows:
        activity.setdefault(machine_id, {"operator": None, "issue": None})
        activity[machine_id]["issue"] = issue

    return activity


def get_latest_entries_for_date(entry_date: date_type) -> list[dict]:
    """Returns the latest entry per (machine_id, time_slot) for a given date.

    This is what /dashboard renders into the grid. "Latest" = highest
    created_at, so a correction (new row for the same machine+slot+date)
    naturally supersedes the prior value while the prior row stays in the
    table for history.
    """
    with get_pg_connection() as conn:
        cursor = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, time_slot)
                machine_id, operator, time_slot, units_produced, status,
                issue, entered_by, entry_date, created_at
            FROM entries
            WHERE entry_date = %s
            ORDER BY machine_id, time_slot, created_at DESC
            """,
            (entry_date,),
        )
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []

    return [dict(zip(columns, row)) for row in rows]
