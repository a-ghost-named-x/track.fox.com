"""Data access and OEE computation for /oee.

OEE = Availability x Performance x Quality, computed per machine per shift.

Downtime and scrap are entered once per shift on /console/oee, while good
units come from the 2-hour rounds. Judging at shift level also smooths out
reading-time noise: a checkpoint written late borrows units from its
neighbour, but a shift is the same length however the readings fell.

The OEE tables are separate from `entries` because the grid resolves each
cell with "newest row wins". A scrap row on `entries` would become the newest
row for that cell and blank out the units entered earlier.

Shift length
------------
Each machine's shift can be 8, 10 or 12 hours, or 1-6 on a short day (see
"Shift length" in app/models.py). The length decides which checkpoints the
shift has, how many minutes it is, and whether it exists at all (there is no
3rd Shift after a 12-hour 2nd). On a short day the minutes are the scheduled
hours x 60, not checkpoints x 120.

The production day
------------------
compute_oee_report(D) covers the day that starts at 6AM on D, including the
3rd Shift from D 10PM to D+1 6AM. OEE rows are filed under D, but the rounds
file 12AM-6AM readings under D+1, so both dates are read and
shift_slot_dates() says which date each checkpoint carries.

Formulas
--------
Per machine per shift (elapsed minutes = 480 for a finished 8-hour shift):

    good        = last production reading in the shift (see _cumulative_deltas)
    total       = good + scrap
    PPT         = elapsed minutes - planned downtime
    run time    = PPT - unplanned downtime

    Availability = run time / PPT
    Performance  = total / (ideal rate x run time)
    Quality      = good / total
    OEE          = A x P x Q

Run time cancels out of the product, so there's a second route to the same
number:

    OEE = good / (ideal rate x PPT)

`oee` uses the short route and `oee_from_factors` the long one.
tests/test_oee_math.py asserts they agree for single machines and rollups.

Across machines, Availability is weighted by capacity rather than clock
minutes, because machines run at different rates. See _aggregate().

What each number needs
----------------------
Scrap cancels out of A x P x Q, so OEE doesn't need it:

    production + downtime            -> OEE and Availability
    production + downtime + scrap    -> the full A / P / Q split

Rules
-----
- No defaulting. Missing downtime isn't zero downtime and missing scrap isn't
  perfect quality; both become None and show as N/A. (A blank production
  checkpoint is different and does mean zero; see _cumulative_deltas.)
- No averaging of percentages. Rollups sum counts and minutes, then divide.
- No clamping. A Performance over 100% is shown as-is.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta

from app.db.postgres import get_pg_connection
from app.models import (
    DASHBOARD_ZONES,
    DEFAULT_SHIFT_HOURS,
    MACHINE_IDS,
    SHIFT_MINUTES,
    SHIFT_ORDER,
    SHIFT_SLOTS,
    SLOT_MINUTES,
    ScheduleCreate,
    ShiftDowntimeCreate,
    ShiftLengthCreate,
    ShiftScrapCreate,
    default_scheduled,
    elapsed_dated_slots,
    resolve_day_lengths,
    shift_slot_dates,
    shift_span,
    window_span,
)


class UnknownReasonCodeError(Exception):
    """A downtime submission names a code that doesn't exist. Without its
    is_planned flag the minutes can't be classified, so it's rejected."""


class DowntimeExceedsShiftError(Exception):
    """A shift's downtime minutes add up to more than the shift's length.
    Rejected at write time because it would give a negative run time."""


# How far past its ideal-rate ceiling a shift's production must be before
# it's treated as a typo and excluded. The ceiling is derived from standard /
# 0.75, which is itself an estimate, so merely beating it is only a warning.
# Double the ceiling is more likely a transposed digit.
IMPLAUSIBLE_CEILING_MULTIPLE: float = 2.0


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def get_downtime_reasons(*, active_only: bool = True) -> dict[str, dict]:
    """Reason codes keyed by code, in sort_order (the order the form shows).

    `active_only=False` includes retired codes, which older rows still
    reference and which still need a label on /oee.
    """
    query = "SELECT code, label, is_planned, sort_order FROM downtime_reasons"
    if active_only:
        query += " WHERE active"
    query += " ORDER BY sort_order"

    with get_pg_connection() as conn:
        rows = conn.execute(query).fetchall()

    return {
        code: {"code": code, "label": label, "is_planned": is_planned}
        for code, label, is_planned, _ in rows
    }


def get_ideal_rates() -> dict[str, float]:
    """Per-machine theoretical maximum, in units per hour.

    Seeded as standard / 0.75. A machine missing from this table gets no OEE
    rather than a guessed rate. Values are cast to float because psycopg
    returns NUMERIC as Decimal, which isn't JSON-serialisable.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT machine_id, ideal_units_per_hour FROM machine_ideal_rates"
        ).fetchall()
    return {machine_id: float(rate) for machine_id, rate in rows}


def get_shift_standards() -> dict[tuple[str, str], int]:
    """Target good units per (machine_id, shift).

    `standards` is cumulative and resets every shift, so a shift's target is
    the value at its last slot (46,800 at 2PM for C1).
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT machine_id, time_slot, standard_units FROM standards"
        ).fetchall()
    by_slot = {(machine_id, slot): units for machine_id, slot, units in rows}

    return {
        (machine_id, shift): by_slot[(machine_id, slots[-1])]
        for shift, slots in SHIFT_SLOTS.items()
        for machine_id in MACHINE_IDS
        if (machine_id, slots[-1]) in by_slot
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_shift_scrap(payload: ShiftScrapCreate) -> int:
    """Appends a scrap submission for one machine for one shift. A correction
    is a new row; the reader takes the newest per (machine, date, shift)."""
    with get_pg_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO shift_scrap
                (machine_id, entry_date, shift, scrap_units, entered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.entry_date,
                payload.shift,
                payload.scrap_units,
                payload.entered_by,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def create_shift_downtime(
    payload: ShiftDowntimeCreate, *, shift_minutes: int = SHIFT_MINUTES
) -> int:
    """Appends a downtime submission (header plus one row per reason).

    `shift_minutes` is this machine's shift length on this day (480, 600,
    720, or as little as 60 on a short day) and caps the total.

    An empty `payload.reasons` writes a header with no children, which records
    "ran clean". That's why downtime is a header table plus children rather
    than a nullable column: no header means nobody has entered the shift.

    `was_planned` is copied onto each child from the code's current
    is_planned, so changing a code later doesn't re-rate historical OEE.

    Header and children are written in one transaction.

    Codes are checked against the full table, retired ones included. The form
    only offers retired codes on shifts that already have them, so a
    correction to an older shift keeps those minutes instead of dropping them.
    """
    reasons = get_downtime_reasons(active_only=False)

    unknown = [r.reason_code for r in payload.reasons if r.reason_code not in reasons]
    if unknown:
        raise UnknownReasonCodeError(
            f"Unknown downtime reason code(s): {', '.join(sorted(set(unknown)))}. "
            "Pick one of the codes offered on the form."
        )

    total_minutes = sum(r.minutes for r in payload.reasons)
    if total_minutes > shift_minutes:
        raise DowntimeExceedsShiftError(
            f"{total_minutes} minutes of downtime doesn't fit in a "
            f"{shift_minutes}-minute shift ({payload.machine_id}, {payload.shift})."
        )

    with get_pg_connection() as conn:
        header = conn.execute(
            """
            INSERT INTO shift_downtime_entry
                (machine_id, entry_date, shift, note, entered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.entry_date,
                payload.shift,
                payload.note,
                payload.entered_by,
            ),
        ).fetchone()
        header_id = header[0]

        for reason in payload.reasons:
            conn.execute(
                """
                INSERT INTO shift_downtime_reason
                    (downtime_entry_id, reason_code, minutes, was_planned)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    header_id,
                    reason.reason_code,
                    reason.minutes,
                    reasons[reason.reason_code]["is_planned"],
                ),
            )

        conn.commit()

    return header_id


def create_schedule_exception(payload: ScheduleCreate) -> int:
    """Appends a scheduling record for one machine, date and shift.

    Usually only exceptions are written, but scheduled=true is accepted too,
    which is how a mistaken not-scheduled mark is undone.
    """
    with get_pg_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO machine_schedule
                (machine_id, entry_date, shift, scheduled, entered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.entry_date,
                payload.shift,
                payload.scheduled,
                payload.entered_by,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def create_shift_length(payload: ShiftLengthCreate) -> int:
    """Appends a shift-length record for one machine, production day and
    shift. The caller checks shift_length_conflict(), since that needs the
    other shifts' lengths.
    """
    with get_pg_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO machine_shift_length
                (machine_id, entry_date, shift, shift_hours, entered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.entry_date,
                payload.shift,
                payload.shift_hours,
                payload.entered_by,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

# Each OEE table has one query, over a range of production days. The
# single-day readers call it with start == end and drop the date from the key.
# The 7/30-day Pareto reads ranges so it doesn't open a connection per day.

def get_shift_lengths_for_range(
    start: date_type, end: date_type
) -> dict[tuple[str, date_type, str], int]:
    """Latest explicitly-set shift length per (machine_id, production day,
    shift), for every day from `start` to `end` inclusive."""
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, entry_date, shift)
                machine_id, entry_date, shift, shift_hours
            FROM machine_shift_length
            WHERE entry_date BETWEEN %s AND %s
            ORDER BY machine_id, entry_date, shift, created_at DESC
            """,
            (start, end),
        ).fetchall()
    return {(machine_id, day, shift): hours for machine_id, day, shift, hours in rows}


def get_shift_lengths(entry_date: date_type) -> dict[tuple[str, str], int]:
    """Latest explicitly-set shift length per (machine_id, shift) for one
    production day. A missing key means inherit; resolve with
    machine_day_lengths().
    """
    return {
        (machine_id, shift): hours
        for (machine_id, _, shift), hours
        in get_shift_lengths_for_range(entry_date, entry_date).items()
    }


def machine_day_lengths(
    lengths: dict[tuple[str, str], int], machine_id: str
) -> dict[str, int]:
    """One machine's length for every shift of the day, inheritance applied.
    `lengths` is get_shift_lengths()' result for that day."""
    return resolve_day_lengths(
        {shift: hours for (m, shift), hours in lengths.items() if m == machine_id}
    )


def get_latest_scrap_for_date(entry_date: date_type) -> dict[tuple[str, str], int]:
    """Latest scrap total per (machine_id, shift) for one date."""
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, shift)
                machine_id, shift, scrap_units
            FROM shift_scrap
            WHERE entry_date = %s
            ORDER BY machine_id, shift, created_at DESC
            """,
            (entry_date,),
        ).fetchall()
    return {(machine_id, shift): scrap for machine_id, shift, scrap in rows}


def get_downtime_for_range(
    start: date_type, end: date_type
) -> dict[tuple[str, date_type, str], dict]:
    """Latest downtime submission per (machine_id, production day, shift),
    for every day from `start` to `end` inclusive.

    The newest header for a shift wins along with all its reasons, which is
    what lets a correction remove a reason entered by mistake.

    LEFT JOIN because a header with no reasons means "ran clean" and must
    come back as a present-but-empty record.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (machine_id, entry_date, shift)
                    id, machine_id, entry_date, shift, note
                FROM shift_downtime_entry
                WHERE entry_date BETWEEN %s AND %s
                ORDER BY machine_id, entry_date, shift, created_at DESC
            )
            SELECT l.machine_id, l.entry_date, l.shift, l.note,
                   r.reason_code, r.minutes, r.was_planned
            FROM latest l
            LEFT JOIN shift_downtime_reason r ON r.downtime_entry_id = l.id
            ORDER BY l.machine_id, l.entry_date, l.shift, r.minutes DESC NULLS LAST
            """,
            (start, end),
        ).fetchall()

    labels = get_downtime_reasons(active_only=False)
    downtime: dict[tuple[str, date_type, str], dict] = {}

    for machine_id, day, shift, note, reason_code, minutes, was_planned in rows:
        record = downtime.setdefault(
            (machine_id, day, shift),
            {"note": note, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0},
        )
        if reason_code is None:
            continue  # header with no reasons: "ran clean"
        record["reasons"].append(
            {
                "code": reason_code,
                "label": labels.get(reason_code, {}).get("label", reason_code),
                "minutes": minutes,
                "is_planned": was_planned,
            }
        )
        if was_planned:
            record["planned_minutes"] += minutes
        else:
            record["unplanned_minutes"] += minutes

    return downtime


def get_latest_downtime_for_date(entry_date: date_type) -> dict[tuple[str, str], dict]:
    """Latest downtime submission per (machine_id, shift) for one production
    day. See get_downtime_for_range() for how a submission is resolved."""
    return {
        (machine_id, shift): record
        for (machine_id, _, shift), record
        in get_downtime_for_range(entry_date, entry_date).items()
    }


def get_schedule_for_range(
    start: date_type, end: date_type
) -> dict[tuple[str, date_type, str], bool]:
    """Latest scheduling record per (machine_id, production day, shift), for
    every day from `start` to `end` inclusive. A missing key means the
    default from default_scheduled()."""
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, entry_date, shift)
                machine_id, entry_date, shift, scheduled
            FROM machine_schedule
            WHERE entry_date BETWEEN %s AND %s
            ORDER BY machine_id, entry_date, shift, created_at DESC
            """,
            (start, end),
        ).fetchall()
    return {
        (machine_id, day, shift): scheduled
        for machine_id, day, shift, scheduled in rows
    }


def get_schedule_for_date(entry_date: date_type) -> dict[tuple[str, str], bool]:
    """Latest scheduling record per (machine_id, shift) for one date."""
    return {
        (machine_id, shift): scheduled
        for (machine_id, _, shift), scheduled
        in get_schedule_for_range(entry_date, entry_date).items()
    }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def _cumulative_deltas(
    cumulative: dict[str, int | None],
    slots: list[str],
    *,
    elapsed: set[str] | None = None,
) -> dict[str, int | None]:
    """Turns a shift's cumulative production checkpoints into per-slot amounts.

    A blank checkpoint means the count hasn't changed, not that it's unknown.
    The floor leaves a box empty when there's nothing new to write (machine
    down, operator moved), so a blank slot produced zero and a blank first
    slot means the counter was still at zero.

    Treating blanks as unknown would flatter OEE, since blanks are a machine's
    worst slots. A shift reading 10500 / 15750 / 15750 / blank scores 25.2%
    with the blank counted as zero, but 33.7% if it's dropped.

    Two guards:
    1. A shift with no readings at all stays unknown. That means nobody
       logged it; a machine that didn't run is marked not scheduled instead.
    2. Slots that haven't elapsed yet are unknown, never zero.
    """
    countable = [s for s in slots if elapsed is None or s in elapsed]
    if all(cumulative.get(slot) is None for slot in countable):
        return {slot: None for slot in slots}

    deltas: dict[str, int | None] = {}
    last_known = 0
    for slot in slots:
        if elapsed is not None and slot not in elapsed:
            deltas[slot] = None
            continue
        current = cumulative.get(slot)
        if current is None:
            deltas[slot] = 0  # blank: count unchanged
        else:
            deltas[slot] = current - last_known
            last_known = current
    return deltas


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    """Division that returns None for an unknown input or a zero denominator,
    rather than inventing a 0% or 100%."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _aggregate(
    *,
    good: int,
    scrap: int | None,
    standard: int,
    ppt: float,
    run: float,
    planned: float,
    unplanned: float,
    ideal_at_ppt: float,
    ideal_at_run: float,
) -> dict:
    """Combines summed components into the ratios.

    Inputs are sums, and each ratio is divided once, so a machine that ran 20
    minutes doesn't weigh the same as one that ran all shift.

    The ideal figures are passed as units (ideal_per_minute x minutes), not
    a rate. Machines run at different rates, so a zone has no single rate but
    does have a total number of units it could have made.

    `availability` is weighted by capacity (ideal units at run time over
    ideal units at PPT). For one machine that equals run / PPT. Across
    machines it's the only weighting under which A x P x Q still equals OEE,
    since a minute on AS1 (1,680/hr) isn't worth a minute on C1 (7,800/hr).
    `uptime` keeps the plain clock-minutes ratio for display.
    """
    total = good + scrap if scrap is not None else None
    availability = _safe_divide(ideal_at_run, ideal_at_ppt)
    performance = _safe_divide(total, ideal_at_run)
    quality = _safe_divide(good, total)
    oee = _safe_divide(good, ideal_at_ppt)

    return {
        "good": good,
        "scrap": scrap,
        "total": total,
        "standard": standard,
        "ppt_minutes": ppt,
        "run_minutes": run,
        "planned_minutes": planned,
        "unplanned_minutes": unplanned,
        "ideal_at_ppt": ideal_at_ppt,
        "ideal_at_run": ideal_at_run,
        "availability": availability,
        "uptime": _safe_divide(run, ppt),
        "performance": performance,
        "quality": quality,
        "oee": oee,
        "pct_of_standard": _safe_divide(good, standard),
        # A x P x Q by the long route. Must equal `oee`; tests check this.
        "oee_from_factors": (
            availability * performance * quality
            if None not in (availability, performance, quality)
            else None
        ),
    }


def _new_accumulator() -> dict:
    """A zeroed set of the summable components a rollup needs."""
    return {
        "good": 0, "scrap": 0, "standard": 0,
        "ppt": 0.0, "run": 0.0, "planned": 0.0, "unplanned": 0.0,
        "ideal_at_ppt": 0.0, "ideal_at_run": 0.0, "machines": 0,
    }


def _accumulate(acc: dict, result: dict, *, with_scrap: bool = False) -> None:
    """Folds one machine's shift result into a rollup accumulator."""
    acc["good"] += result["good"]
    acc["standard"] += result["standard"]
    acc["ppt"] += result["ppt_minutes"]
    acc["run"] += result["run_minutes"]
    acc["planned"] += result["planned_minutes"]
    acc["unplanned"] += result["unplanned_minutes"]
    acc["ideal_at_ppt"] += result["ideal_at_ppt"]
    acc["ideal_at_run"] += result["ideal_at_run"]
    acc["machines"] += 1
    if with_scrap:
        acc["scrap"] += result["scrap"]


def _accumulator_args(acc: dict) -> dict:
    """Maps an accumulator onto _aggregate's keyword arguments."""
    return {
        "good": acc["good"], "standard": acc["standard"],
        "ppt": acc["ppt"], "run": acc["run"],
        "planned": acc["planned"], "unplanned": acc["unplanned"],
        "ideal_at_ppt": acc["ideal_at_ppt"], "ideal_at_run": acc["ideal_at_run"],
    }


def _build_rollup(machines: dict[str, dict], machine_ids: list[str]) -> dict | None:
    """Combines a set of machines' shift results into one rollup (a zone or
    the whole floor).

    Machines that weren't scheduled or have nothing countable are skipped.

    There are two accumulators because the factors cover different machines.
    OEE and Availability need production and downtime; Performance and
    Quality also need scrap. Both machine counts are returned so the page can
    label a partial figure.
    """
    roll = _new_accumulator()
    split = _new_accumulator()

    for machine_id in machine_ids:
        machine = machines.get(machine_id)
        if machine is None or not machine["scheduled"] or machine["shift"] is None:
            continue
        _accumulate(roll, machine["shift"])
        if machine["scrap_known"]:
            _accumulate(split, machine["shift"], with_scrap=True)

    if not roll["machines"]:
        return None

    rollup = _aggregate(scrap=None, **_accumulator_args(roll))
    rollup["machines"] = roll["machines"]
    rollup["split_machines"] = split["machines"]

    if split["machines"]:
        subset = _aggregate(scrap=split["scrap"], **_accumulator_args(split))
        rollup["performance"] = subset["performance"]
        rollup["quality"] = subset["quality"]
        rollup["scrap"] = split["scrap"]
        rollup["total"] = subset["total"]
        # A x P x Q only equals this rollup's OEE when all three factors cover
        # the same machines. If some are missing scrap, report None.
        rollup["oee_from_factors"] = (
            subset["oee_from_factors"]
            if split["machines"] == roll["machines"] else None
        )
        rollup["split_oee"] = subset["oee"]

    return rollup


def _build_pareto(machines: dict[str, dict], machine_ids: list[str]) -> list[dict]:
    """Downtime minutes by reason across a set of machines for one shift.

    Includes every scheduled machine with a downtime submission, even if its
    OEE couldn't be computed.
    """
    pareto: dict[str, dict] = {}
    for machine_id in machine_ids:
        machine = machines.get(machine_id)
        if machine is None or not machine["scheduled"]:
            continue
        for reason in machine["reasons"]:
            bucket = pareto.setdefault(
                reason["code"],
                {
                    "code": reason["code"],
                    "label": reason["label"],
                    "is_planned": reason["is_planned"],
                    "minutes": 0,
                    "machines": 0,
                },
            )
            bucket["minutes"] += reason["minutes"]
            bucket["machines"] += 1
    return _rank_pareto(pareto)


def _rank_pareto(pareto: dict[str, dict]) -> list[dict]:
    """Downtime minutes by reason, biggest first, with each unplanned
    reason's share of the unplanned total."""
    ranked = sorted(pareto.values(), key=lambda item: item["minutes"], reverse=True)
    unplanned_total = sum(i["minutes"] for i in ranked if not i["is_planned"])
    for item in ranked:
        item["pct_of_unplanned"] = (
            item["minutes"] / unplanned_total
            if unplanned_total and not item["is_planned"] else None
        )
    return ranked


def _machine_warnings(result: dict | None, production_warnings: list[str]) -> list[str]:
    """Soft warnings for one machine's shift. The machine still counts.

    Judged on the whole shift, never a single slot: a checkpoint read late
    borrows units from its neighbour, so one slot can look impossible while
    the shift is fine.
    """
    warnings = list(production_warnings)
    if result is not None:
        oee = result.get("oee")
        if oee is not None and oee > 1:
            warnings.append("over_100")
    return warnings


def _summarise_production(
    readings: dict[str, int | None],
    deltas: dict[str, int | None],
    elapsed: list[str],
) -> dict:
    """Reduces a shift's checkpoints to its total, plus data-quality flags.

    `good` is the sum of the deltas, which equals the last reported reading.

      units_went_backwards (warning) - a checkpoint is lower than the one
          before it. If a later reading is higher, the total is still right
          and the machine still counts.

      final_reading_low (flag) - the last reading is below an earlier one, so
          the total itself is wrong. The machine is excluded.
    """
    reported = [readings[slot] for slot in elapsed if readings.get(slot) is not None]
    values = [deltas[slot] for slot in elapsed if deltas.get(slot) is not None]

    if not reported:
        return {"good": None, "flags": [], "warnings": []}

    flags: list[str] = []
    warnings: list[str] = []
    if any(value < 0 for value in values):
        warnings.append("units_went_backwards")
    if reported[-1] < max(reported):
        flags.append("final_reading_low")

    return {"good": sum(values), "flags": flags, "warnings": warnings}


def compute_oee_report(entry_date: date_type, *, now: datetime | None = None) -> dict:
    """OEE for one production day: every machine, all three shifts.

    All three shifts are returned together so /oee's shift toggle works
    without another request.

    Machines that weren't scheduled are left out of the rollups (not scored
    0%). A machine with no ideal rate gets no OEE. A shift that doesn't exist
    for a machine (the 3rd after a 12-hour 2nd) comes back with
    scheduled=False and shift_exists=False, so rollups skip it the same way.

    `entry_date` is the production day; its 3rd Shift starts at 10PM that
    day.
    """
    now = now or datetime.now()

    # Local import avoids a circular import between the two db modules.
    from app.db.entries import get_latest_entries_for_date

    production_day = entry_date
    next_day = production_day + timedelta(days=1)
    lengths = get_shift_lengths(production_day)

    # 12AM-6AM checkpoints are filed under the next calendar date, so read
    # both days.
    units_map: dict[tuple[str, date_type, str], int] = {}
    operator_map: dict[tuple[str, date_type, str], str] = {}
    for day in (production_day, next_day):
        for row in get_latest_entries_for_date(day):
            key = (row["machine_id"], day, row["time_slot"])
            units_map[key] = row["units_produced"]
            operator_map[key] = row["operator"]

    scrap_map = get_latest_scrap_for_date(production_day)
    downtime_map = get_latest_downtime_for_date(production_day)
    schedule_map = get_schedule_for_date(production_day)
    ideal_rates = get_ideal_rates()
    standards = get_shift_standards()
    day_lengths = {m: machine_day_lengths(lengths, m) for m in MACHINE_IDS}

    shifts: dict[str, dict] = {}

    for shift_label in SHIFT_ORDER:
        # The 8-hour view, for the page's "N elapsed slots" note. Each
        # machine also carries its own count.
        default_elapsed = elapsed_dated_slots(
            shift_slot_dates(shift_label, production_day) or [], now
        )
        machines: dict[str, dict] = {}

        for machine_id in MACHINE_IDS:
            hours = day_lengths[machine_id][shift_label]
            dated_slots = shift_slot_dates(shift_label, production_day, hours)
            if dated_slots is None:
                machines[machine_id] = _absent_shift(
                    hours, production_day, day_lengths[machine_id]
                )
                continue

            slots = [slot for slot, _ in dated_slots]
            elapsed = elapsed_dated_slots(dated_slots, now)
            elapsed_set = set(elapsed)

            scheduled = schedule_map.get(
                (machine_id, shift_label), default_scheduled(shift_label, hours)
            )
            ideal_per_hour = ideal_rates.get(machine_id)
            downtime = downtime_map.get((machine_id, shift_label))
            scrap = scrap_map.get((machine_id, shift_label))

            readings = {
                slot: units_map.get((machine_id, slot_date, slot))
                for slot, slot_date in dated_slots
            }
            deltas = _cumulative_deltas(readings, slots, elapsed=elapsed_set)
            production = _summarise_production(readings, deltas, elapsed)

            flags = list(production["flags"])
            if ideal_per_hour is None:
                flags.append("no_ideal_rate")

            # Planned Production Time is the elapsed part of the shift, minus
            # planned downtime (currently always zero; no reason codes are
            # planned).
            #
            # A short-day shift is capped at its scheduled hours: 4 hours on
            # the 1st Shift is 240 minutes even though its window is six. For
            # 8/10/12 hours the cap is the whole window.
            planned = downtime["planned_minutes"] if downtime else 0
            unplanned = downtime["unplanned_minutes"] if downtime else 0
            elapsed_minutes = min(SLOT_MINUTES * len(elapsed), hours * 60)
            ppt = elapsed_minutes - planned if downtime else None
            run = ppt - unplanned if ppt is not None else None

            if run is not None and run < 0:
                flags.append("downtime_over_shift")

            ideal_per_minute = (ideal_per_hour or 0) / 60
            good = production["good"]

            if (
                good is not None
                and ppt
                and good > ideal_per_minute * ppt * IMPLAUSIBLE_CEILING_MULTIPLE
            ):
                flags.append("implausible_units")

            countable = (
                good is not None
                and downtime is not None
                and ideal_per_hour is not None
                and not flags
            )
            scrap_known = countable and scrap is not None

            # The stored standard is an 8-hour target; scale it to the
            # shift's hours. Every 2-hour increment is even, so the result
            # stays whole.
            standard = (
                standards.get((machine_id, shift_label), 0) * hours // DEFAULT_SHIFT_HOURS
            )

            result = None
            if countable:
                result = _aggregate(
                    good=good,
                    scrap=scrap if scrap_known else None,
                    standard=standard,
                    ppt=ppt,
                    run=run,
                    planned=planned,
                    unplanned=unplanned,
                    ideal_at_ppt=ideal_per_minute * ppt,
                    ideal_at_run=ideal_per_minute * run,
                )

            machines[machine_id] = {
                "scheduled": scheduled,
                "shift_exists": True,
                "shift_hours": hours,
                "shift_minutes": hours * 60,
                "span": shift_span(shift_label, hours),
                # Differs from `span` only on a short day (6AM-12PM window
                # around a scheduled 6AM-10AM).
                "window_span": window_span(shift_label, hours),
                "production_day": production_day.isoformat(),
                "operator": next(
                    (
                        operator_map[(machine_id, slot_date, slot)]
                        for slot, slot_date in reversed(dated_slots)
                        if (machine_id, slot_date, slot) in operator_map
                    ),
                    None,
                ),
                "shift": result,
                # Raw checkpoints, so a flagged machine can be diagnosed from
                # the page. Each carries the calendar date it was filed under.
                "checkpoints": [
                    {
                        "slot": slot,
                        "date": slot_date.isoformat(),
                        "reading": readings[slot],
                        "produced": deltas[slot],
                        "reported": readings[slot] is not None,
                        "elapsed": slot in elapsed_set,
                    }
                    for slot, slot_date in dated_slots
                ],
                "reasons": downtime["reasons"] if downtime else [],
                "note": downtime["note"] if downtime else None,
                "has_production": good is not None,
                "has_downtime": downtime is not None,
                "has_scrap": scrap is not None,
                "scrap": scrap,
                "scrap_known": scrap_known,
                "elapsed_minutes": elapsed_minutes,
                "elapsed_count": len(elapsed),
                "flags": sorted(set(flags)),
                "warnings": _machine_warnings(result, production["warnings"]),
            }

        shifts[shift_label] = {
            "machines": machines,
            "rollup": _build_rollup(machines, MACHINE_IDS),
            "zones": {
                slug: _build_rollup(machines, zone_machines)
                for slug, zone_machines in DASHBOARD_ZONES.items()
            },
            "pareto": _build_pareto(machines, MACHINE_IDS),
            "completeness": _completeness(machines, default_elapsed),
            "elapsed_slots": default_elapsed,
            "production_day": production_day.isoformat(),
        }

    return {
        "date": entry_date.isoformat(),
        "shifts": shifts,
        # The 8-hour default. Each machine carries its own shift_minutes.
        "shift_minutes": SHIFT_MINUTES,
    }


def _absent_shift(hours: int, production_day: date_type, day_lengths: dict[str, int]) -> dict:
    """The row for a shift the day has no room for (the 3rd Shift after a 10
    or 12-hour 2nd).

    scheduled=False keeps it out of every rollup. shift_exists=False and
    `covered_by` let the page say "no 3rd shift" and why. Anything entered
    under that shift is ignored, because those hours belong to the 2nd Shift.
    """
    previous = SHIFT_ORDER[-2]
    return {
        "scheduled": False,
        "shift_exists": False,
        "shift_hours": hours,
        "shift_minutes": 0,
        "span": None,
        "window_span": None,
        "covered_by": {
            "shift": previous,
            "hours": day_lengths[previous],
            "span": shift_span(previous, day_lengths[previous]),
        },
        "production_day": production_day.isoformat(),
        "operator": None,
        "shift": None,
        "checkpoints": [],
        "reasons": [],
        "note": None,
        "has_production": False,
        "has_downtime": False,
        "has_scrap": False,
        "scrap": None,
        "scrap_known": False,
        "elapsed_minutes": 0,
        "elapsed_count": 0,
        "flags": [],
        "warnings": [],
    }


def _completeness(machines: dict[str, dict], elapsed: list[str]) -> dict:
    """How much of the shift's data has been entered, counted per column.

    Production and OEE data are entered at different times, so separate
    counts ("Downtime 12/34") say what's missing better than one percentage.
    A machine counts for production once it has any reading.

    `elapsed` is the 8-hour view of the shift, for the page's note.
    `other_length_machines` counts scheduled machines not on 8 hours.
    """
    counts = {"production": 0, "downtime": 0, "scrap": 0}
    total = 0
    other_length = 0

    for machine in machines.values():
        if not machine["scheduled"]:
            continue
        total += 1
        if machine["shift_hours"] != DEFAULT_SHIFT_HOURS:
            other_length += 1
        if not machine["elapsed_count"]:
            continue
        if machine["has_production"]:
            counts["production"] += 1
        if machine["has_downtime"]:
            counts["downtime"] += 1
        if machine["has_scrap"]:
            counts["scrap"] += 1

    return {
        "machines": total,
        "slots_expected": len(elapsed),
        "other_length_machines": other_length,
        "production": counts["production"],
        "downtime": counts["downtime"],
        "scrap": counts["scrap"],
    }


# ---------------------------------------------------------------------------
# The downtime Pareto over a week or a month
# ---------------------------------------------------------------------------

def compute_pareto_range(
    end_day: date_type, days: int, *, now: datetime | None = None
) -> dict:
    """Downtime minutes by reason for every machine, summed over the `days`
    production days ending on (and including) `end_day`, all shifts
    together. Used by /oee's 7 and 30-day Pareto.

    Returned per machine so the page's machine picker can filter in the
    browser. A machine-shift counts if it exists on that day and is
    scheduled, the same rule compute_oee_report() uses.

    Each machine also carries coverage counts, since a month with unentered
    downtime would otherwise look better than it was:

      records - counted machine-shifts with a downtime submission
      missing - finished machine-shifts that reported production but have no
                downtime submission

    Reported production is the test for whether a shift ran, so an unworked
    Sunday isn't counted as missing.

    Reads each table once for the whole range rather than building a report
    per day, which would open hundreds of connections.
    """
    from app.db.entries import get_reported_slots

    now = now or datetime.now()
    start = end_day - timedelta(days=days - 1)

    explicit_lengths = get_shift_lengths_for_range(start, end_day)
    schedule = get_schedule_for_range(start, end_day)
    downtime = get_downtime_for_range(start, end_day)
    # The last day's night checkpoints are filed under the morning after it.
    reported = get_reported_slots(start, end_day + timedelta(days=1))

    explicit_by_day: dict[tuple[str, date_type], dict[str, int]] = {}
    for (machine_id, day, shift), hours in explicit_lengths.items():
        explicit_by_day.setdefault((machine_id, day), {})[shift] = hours

    machines: dict[str, dict] = {}
    for machine_id in MACHINE_IDS:
        reasons: dict[str, dict] = {}
        records = 0
        missing = 0

        for offset in range(days):
            day = start + timedelta(days=offset)
            lengths = resolve_day_lengths(explicit_by_day.get((machine_id, day), {}))

            for shift in SHIFT_ORDER:
                hours = lengths[shift]
                dated_slots = shift_slot_dates(shift, day, hours)
                if dated_slots is None:
                    continue  # shift doesn't exist on this day
                scheduled = schedule.get(
                    (machine_id, day, shift), default_scheduled(shift, hours)
                )
                if not scheduled:
                    continue

                record = downtime.get((machine_id, day, shift))
                if record is None:
                    over = len(elapsed_dated_slots(dated_slots, now)) == len(dated_slots)
                    ran = any(
                        (machine_id, slot_date, slot) in reported
                        for slot, slot_date in dated_slots
                    )
                    if over and ran:
                        missing += 1
                    continue

                records += 1
                for reason in record["reasons"]:
                    bucket = reasons.setdefault(
                        reason["code"],
                        {
                            "code": reason["code"],
                            "label": reason["label"],
                            "is_planned": reason["is_planned"],
                            "minutes": 0,
                            "shifts": 0,
                        },
                    )
                    bucket["minutes"] += reason["minutes"]
                    bucket["shifts"] += 1

        machines[machine_id] = {
            "reasons": sorted(reasons.values(), key=lambda r: r["minutes"], reverse=True),
            "records": records,
            "missing": missing,
        }

    return {
        "start": start.isoformat(),
        "end": end_day.isoformat(),
        "days": days,
        "machines": machines,
    }
