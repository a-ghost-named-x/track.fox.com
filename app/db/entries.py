"""Reads and writes for the 2-hour production entries.

An entry's status is computed once, at write time, by comparing
units_produced with that machine and slot's standard, and stored on the row.
"""
from datetime import date as date_type
from datetime import timedelta

from app.db.postgres import get_pg_connection
from app.models import NIGHT_SLOTS, Entry, EntryCreate


class StandardNotFoundError(Exception):
    """No standard exists for a machine_id + time_slot, so status can't be
    computed. Surfaced to the user rather than guessing a status."""


def get_standard(machine_id: str, time_slot: str) -> int:
    with get_pg_connection() as conn:
        row = conn.execute(
            "SELECT standard_units FROM standards WHERE machine_id = %s AND time_slot = %s",
            (machine_id, time_slot),
        ).fetchone()
    if row is None:
        raise StandardNotFoundError(
            f"No standard defined for machine_id={machine_id!r} time_slot={time_slot!r}. "
            "Add it to the standards table before logging entries for this slot."
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


def _collapse_issue_runs(reported: list[tuple[str, str]]) -> list[dict]:
    """Merges consecutive slots reporting the same issue into one item.

    The floor often re-types the same issue on every slot it's still true
    for, so `[("8AM", "defective material"), ("10AM", "defective material")]`
    becomes one item spanning 8AM through 10AM.

    Matching ignores case and extra whitespace; the first spelling is kept.
    "Consecutive" means among the slots that reported an issue, so 8AM and
    12PM with a silent 10AM still collapse to "8AM-12PM".

    `reported` must already be in slot order.
    """
    collapsed: list[dict] = []
    for time_slot, issue in reported:
        key = " ".join(issue.split()).casefold()
        if collapsed and collapsed[-1]["_key"] == key:
            collapsed[-1]["through"] = time_slot
            continue
        collapsed.append(
            {"_key": key, "time_slot": time_slot, "through": None, "issue": issue}
        )

    for item in collapsed:
        del item["_key"]
    return collapsed


def get_shift_activity(entry_date: date_type, active_slots: list[str]) -> dict[str, dict]:
    """Per machine, the operator and the issues for one shift's slots.

    - operator: from the machine's most recent entry in the shift.
    - issues: every slot that reported a non-empty issue, in slot order, as
      `{time_slot, through, issue}` items (see _collapse_issue_runs()).

    An entry with a blank issue doesn't clear an earlier one, because empty
    issues are filtered out before DISTINCT ON picks the newest. A number
    correction therefore leaves the original issue in place.
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

        # One issue per slot, so a correction supersedes within its own slot
        # the same way the numbers do.
        issue_rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, time_slot) machine_id, time_slot, issue
            FROM entries
            WHERE entry_date = %s AND time_slot = ANY(%s)
              AND issue IS NOT NULL AND issue <> ''
            ORDER BY machine_id, time_slot, created_at DESC
            """,
            (entry_date, active_slots),
        ).fetchall()

    activity: dict[str, dict] = {
        machine_id: {"operator": operator, "issues": []}
        for machine_id, operator in operator_rows
    }

    reported: dict[str, list[tuple[str, str]]] = {}
    for machine_id, time_slot, issue in issue_rows:
        reported.setdefault(machine_id, []).append((time_slot, issue))

    # Sort by slot order, not created_at: a late correction to the 8AM number
    # still belongs on the 8AM line.
    slot_order = {slot: index for index, slot in enumerate(active_slots)}
    for machine_id, rows in reported.items():
        rows.sort(key=lambda row: slot_order.get(row[0], len(slot_order)))
        activity.setdefault(machine_id, {"operator": None, "issues": []})
        activity[machine_id]["issues"] = _collapse_issue_runs(rows)

    return activity


def get_latest_entries_for_date(entry_date: date_type) -> list[dict]:
    """The latest entry per (machine_id, time_slot) for a calendar date.

    The newest row wins, so a correction supersedes the earlier value while
    the earlier row stays in the table as history.
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


def get_available_entry_dates() -> list[date_type]:
    """Every calendar date with at least one entry, newest first."""
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT entry_date FROM entries ORDER BY entry_date DESC"
        ).fetchall()

    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Production-day readers for the review pages (/supervisor, /oee)
#
# The rounds file 12AM-6AM checkpoints under the morning they land on. The
# review pages work in production days (6AM to 6AM), so these read the night
# slots from the next calendar date.
# ---------------------------------------------------------------------------

def get_production_day_entries(production_day: date_type) -> list[dict]:
    """Latest entry per (machine_id, time_slot) for one production day: the
    day slots from `production_day` and the night slots from the morning
    after. Rows keep the calendar entry_date they were filed under."""
    next_day = production_day + timedelta(days=1)
    night = set(NIGHT_SLOTS)
    rows = [r for r in get_latest_entries_for_date(production_day) if r["time_slot"] not in night]
    rows += [r for r in get_latest_entries_for_date(next_day) if r["time_slot"] in night]
    return rows


def get_available_production_days() -> list[date_type]:
    """Every production day with at least one entry, newest first. A night
    slot counts towards the day before the date it was filed under."""
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT
                CASE WHEN time_slot = ANY(%s) THEN entry_date - 1 ELSE entry_date END
                    AS production_day
            FROM entries
            ORDER BY production_day DESC
            """,
            (NIGHT_SLOTS,),
        ).fetchall()
    return [row[0] for row in rows]


def get_reported_slots(
    start: date_type, end: date_type
) -> set[tuple[str, date_type, str]]:
    """Every (machine_id, calendar entry_date, time_slot) with at least one
    entry between `start` and `end` inclusive.

    Presence only. The 7/30-day Pareto uses it to tell a shift that ran from
    one nobody worked, so only the first can count as missing its downtime.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT machine_id, entry_date, time_slot
            FROM entries
            WHERE entry_date BETWEEN %s AND %s
            """,
            (start, end),
        ).fetchall()
    return {(machine_id, day, slot) for machine_id, day, slot in rows}
