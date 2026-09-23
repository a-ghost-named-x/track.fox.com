"""Data access and business logic for the manual-entry dashboard.

Houses the status-computation rule: an entry's status is derived from
comparing units_produced against that machine+time_slot's standard, at
write-time, and stored on the row (per the decision to snapshot status
rather than recompute it on every dashboard read).
"""
from datetime import date as date_type
from datetime import timedelta

from app.db.postgres import get_pg_connection
from app.models import NIGHT_SLOTS, Entry, EntryCreate


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


def _collapse_issue_runs(reported: list[tuple[str, str]]) -> list[dict]:
    """Merges consecutive slots reporting the same issue into a single item.

    Carry-forward means the floor often re-types the same text on every slot
    it's still true for, which would otherwise stack up as four near-identical
    lines in one cell. `[("8AM", "defective material"), ("10AM", "defective
    material")]` collapses to one item spanning 8AM through 10AM, which is
    both shorter and a truer statement of what happened.

    Two details worth knowing:

    - Matching ignores case and runs of whitespace, so "Defective material"
      typed by one operator merges with "defective material" typed by the
      next. The text displayed is the first spelling of the run.
    - "Consecutive" means consecutive among the slots that actually REPORTED
      an issue, not adjacent in the shift. An issue at 8AM and 12PM with a
      silent 10AM between them still collapses to "8AM-12PM" — under the
      carry-forward rule the issue was in effect at 10AM too, nobody just
      re-typed it.

    `reported` must already be in slot order; the caller sorts it.
    """
    collapsed: list[dict] = []
    for time_slot, issue in reported:
        key = " ".join(issue.split()).casefold()
        if collapsed and collapsed[-1]["_key"] == key:
            # Same issue still running — stretch the previous item's range
            # instead of opening a new line for it.
            collapsed[-1]["through"] = time_slot
            continue
        collapsed.append(
            {"_key": key, "time_slot": time_slot, "through": None, "issue": issue}
        )

    for item in collapsed:
        del item["_key"]
    return collapsed


def get_shift_activity(entry_date: date_type, active_slots: list[str]) -> dict[str, dict]:
    """Returns, per machine, the operator and the issues in effect for the
    shift in progress (i.e. whichever of `active_slots` have entries so far
    today).

    - operator: from that machine's most recently created entry anywhere in
      the shift (whichever slot it was logged against) — "who's on it right
      now", shown once per machine row rather than repeated per cell.
    - issues: EVERY slot in the shift that reported a non-empty issue, in
      slot order, as `{time_slot, through, issue}` items — `through` is set
      only when consecutive slots reported the same thing (see
      _collapse_issue_runs). The display renders one line per item, prefixed
      with its slot, so "defective material at 8AM" and "belt slip at 2PM"
      stay distinguishable inside the single Issue cell per machine row.

      This used to be a single string: DISTINCT ON (machine_id) took the
      newest issue in the shift and every earlier one was dropped on the
      floor. Nothing about carry-forward changes here — an issue reported at
      8AM still shows for the rest of the shift; it's now an explicit line
      that says 8AM rather than an unlabelled string that silently outlived
      its slot.

      An entry logged with a blank issue does NOT clear an earlier one — the
      WHERE filter drops empty issues before DISTINCT ON picks a winner, so
      a correction filed to fix a number (the batch form never pre-fills the
      issue box) leaves the original issue standing. Same as the previous
      behaviour: there is still no way to retract an issue mid-shift short of
      the shift changing over. That's also the one case where a line here has
      no matching corner marker on its slot cell, since the marker follows
      the latest entry per slot.
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

        # DISTINCT ON (machine_id, time_slot), not just (machine_id): one
        # issue per slot rather than one per machine, so a correction still
        # supersedes within its own slot the way get_latest_entries_for_date()
        # handles the numbers.
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

    # Chronological by `active_slots`, which the caller passes in shift order
    # (SHIFT_SLOTS), NOT by created_at — a 4PM correction to the 8AM number
    # belongs on the 8AM line, above 10AM's, not at the bottom of the cell.
    slot_order = {slot: index for index, slot in enumerate(active_slots)}
    for machine_id, rows in reported.items():
        rows.sort(key=lambda row: slot_order.get(row[0], len(slot_order)))
        activity.setdefault(machine_id, {"operator": None, "issues": []})
        activity[machine_id]["issues"] = _collapse_issue_runs(rows)

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


def get_available_entry_dates() -> list[date_type]:
    """Every production date that has at least one entry, newest first.

    Backs /supervisor's date picker: the newest and oldest values clamp the
    date box's max/min so a date with no possible data (last January, 1999)
    can't be picked in the first place, and the first few feed its quick-pick
    buttons.

    DISTINCT over idx_entries_date, so this stays cheap as the table grows —
    it returns one row per production day (a few hundred a year), not one per
    entry. Note it lists only days that have data, so interior gaps (a quiet
    Sunday, a holiday) are absent from the list even though they fall inside
    the min/max range the date box allows. /supervisor handles that by
    showing an explicit "nothing recorded" state for such a day rather than
    trying to make them unpickable — an HTML date input can clamp the ends of
    a range but can't disable dates in the middle of it.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT entry_date FROM entries ORDER BY entry_date DESC"
        ).fetchall()

    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Production-day readers, for the review side (/supervisor, /oee)
#
# The rounds and the floor screens date the 12AM-6AM checkpoints by the
# morning they land on — the two functions above are that calendar-day view,
# and /dashboard depends on it. The review pages think in PRODUCTION DAYS
# instead: 6AM to 6AM, "3rd Shift Thursday" being Thursday night. These two
# translate. Nothing in `entries` changes; the night slots are simply read
# from the next calendar date. (Why the rounds weren't re-keyed instead:
# app/models.py, "THE PRODUCTION DAY".)
# ---------------------------------------------------------------------------

def get_production_day_entries(production_day: date_type) -> list[dict]:
    """Latest entry per (machine_id, time_slot) for one production day: the
    day slots (8AM-10PM) from `production_day` itself and the night slots
    (12AM-6AM) from the morning after. Same row shape as
    get_latest_entries_for_date(), so the grids render it unchanged; each
    row's entry_date is still the calendar date the rounds filed it under."""
    next_day = production_day + timedelta(days=1)
    night = set(NIGHT_SLOTS)
    rows = [r for r in get_latest_entries_for_date(production_day) if r["time_slot"] not in night]
    rows += [r for r in get_latest_entries_for_date(next_day) if r["time_slot"] in night]
    return rows


def get_available_production_days() -> list[date_type]:
    """Every production day with at least one entry, newest first — the
    production-day twin of get_available_entry_dates(), for the review
    pages' date pickers. A night slot counts towards the day BEFORE the
    calendar date it was filed under, so at 3AM the running day is still
    yesterday's rather than a new entry in the list."""
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
    entry, for calendar dates `start` to `end` inclusive.

    Presence only, no values: the 7/30-day Pareto uses it to tell a shift
    that ran (some checkpoint was reported) from one nobody worked, so that
    only the first can count as "missing its downtime". Any row will do —
    a correction can change a reading but never retract one, so an entry
    existing at all means the machine reported. Calendar dates, like the
    rows themselves; the caller maps night slots with shift_slot_dates().
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
