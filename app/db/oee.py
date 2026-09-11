"""Data access and OEE computation for /oee.

OEE = Availability x Performance x Quality, computed per machine per SHIFT.

WHY SHIFT GRAIN
---------------
Downtime and scrap used to be captured every two hours alongside production.
Per the floor (2026-09-09) that was adding work to the rounds, so one person now
enters the whole shift's OEE data in a single sitting at the end of it, on
/console/oee. The 2-hour rounds went back to good units only.

That is a better fit for the metric as well as for the people. A per-slot delta
is the gap between two hand-taken readings, so a reading logged late borrows
units from its neighbour — noise that produced false "impossible value" alarms
on WS1 and C8 in the first week of use. A shift is 480 minutes no matter when
anyone wrote anything down, so the noise cancels completely.

WHY THESE TABLES ARE SEPARATE FROM `entries`
---------------------------------------------
Production and OEE data are entered by different people at different times.
get_latest_entries_for_date() resolves the dashboard grid with DISTINCT ON
(machine_id, time_slot) ORDER BY created_at DESC — the newest ROW wins
wholesale — so a scrap submission landing on `entries` would become the newest
row for that cell and blank the units the production person entered hours
earlier. Separate tables let both write freely and never collide.

THE FORMULAS
------------
Per machine per shift (SHIFT_MINUTES = 480 for a completed shift):

    good        = last production reading in the shift (see _cumulative_deltas)
    total       = good + scrap
    PPT         = elapsed minutes - planned downtime
    run time    = PPT - unplanned downtime

    Availability = run time / PPT
    Performance  = total / (ideal rate x run time)
    Quality      = good / total
    OEE          = A x P x Q

Run time cancels out of that product entirely, giving a second route to the
same number:

    OEE = good / (ideal rate x PPT)

Both are computed for every result — `oee` takes the short route,
`oee_from_factors` the long one — and they must agree. tests/test_oee_math.py
asserts the identity at machine AND rollup level; it is what caught the
capacity-weighted availability bug.

Rolling up across machines, Availability switches from clock minutes to
capacity, because 34 machines run at 8 different rates and a minute on AS1 is
not worth a minute on C1. See _aggregate().

WHAT EACH NUMBER NEEDS
----------------------
Because scrap cancels out of A x P x Q, OEE does NOT need scrap:

    production + downtime            -> OEE and Availability
    production + downtime + scrap    -> the full A / P / Q split

So a shift missing its scrap still yields a real OEE, with Performance and
Quality reported as N/A rather than guessed.

WHAT IS NEVER DONE
------------------
- No defaulting. A missing downtime submission does NOT mean zero downtime, and
  missing scrap does NOT mean perfect quality. Both yield None, and None
  propagates to an "N/A" on the page. (A blank PRODUCTION checkpoint is
  different and does mean zero — see _cumulative_deltas.)
- No averaging of percentages. Every rollup sums the underlying counts and
  minutes and divides once.
- No clamping. A Performance over 100% is surfaced, not squashed.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime

from app.db.postgres import get_pg_connection
from app.models import (
    DASHBOARD_ZONES,
    MACHINE_IDS,
    SHIFT_MINUTES,
    SHIFT_ORDER,
    SHIFT_SLOTS,
    SLOT_MINUTES,
    ScheduleCreate,
    ShiftDowntimeCreate,
    ShiftScrapCreate,
    elapsed_slots,
)


class UnknownReasonCodeError(Exception):
    """Raised when a downtime submission names a code that isn't active.

    A real "we can't classify this" situation: without is_planned we cannot
    tell whether the minutes belong in the OEE denominator, so the submission
    is rejected rather than guessed at.
    """


class DowntimeExceedsShiftError(Exception):
    """Raised when a shift's downtime minutes sum past SHIFT_MINUTES.

    An 8-hour shift cannot contain 500 minutes of downtime. Caught at write
    time because the alternative is a negative run time that quietly poisons
    every rollup it touches.
    """


# How far past its derived ceiling a shift has to be before its production is
# treated as broken rather than merely surprising.
#
# The ceiling is standard / 0.75, so it carries real uncertainty: a machine
# whose standard is actually 85% of maximum will legitimately exceed it. That
# is a warning, not an exclusion — the ceiling is the less trustworthy of the
# two numbers, and an earlier version of this file discarded genuine production
# on seven machines by assuming otherwise. Doubling it, though, is not a rate
# disagreement: it is a transposed digit.
IMPLAUSIBLE_CEILING_MULTIPLE: float = 2.0


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def get_downtime_reasons(*, active_only: bool = True) -> dict[str, dict]:
    """Reason codes keyed by code, ordered as the entry form should show them.

    Python dicts preserve insertion order, so ORDER BY sort_order here is what
    orders the form — the template iterates without re-sorting, and sort_order
    is maintained to match the floor's paper sheet.

    `active_only=False` is for LABELLING history: codes retired by
    12_seed_shift_downtime_reasons.sql are still referenced by migrated rows
    and must still resolve to a human name on /oee.
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

    Seeded by docs/sql/08_seed_ideal_rates.sql as standard / 0.75. A machine
    missing from this table gets no OEE at all rather than a guessed rate —
    the same "refuse rather than assume" stance get_standard() takes.

    Cast to float on the way out: psycopg returns Decimal for NUMERIC, which
    isn't JSON-serialisable, and these are values like 7800.00.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT machine_id, ideal_units_per_hour FROM machine_ideal_rates"
        ).fetchall()
    return {machine_id: float(rate) for machine_id, rate in rows}


def get_shift_standards() -> dict[tuple[str, str], int]:
    """Target good units per (machine_id, shift).

    `standards` is cumulative and resets every shift, so a shift's whole target
    is simply the value at its LAST slot — 46,800 at 2PM for C1, and the same
    number again at 10PM and 6AM.
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
    """Appends a scrap submission for one machine for one shift.

    Append-only: a correction is a new row with a later created_at, and the
    reader takes the newest per (machine, date, shift).
    """
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


def create_shift_downtime(payload: ShiftDowntimeCreate) -> int:
    """Appends a downtime submission (header plus one row per reason).

    An empty `payload.reasons` writes a header with no children, which is how
    "ran clean, no downtime" is recorded — a materially different statement
    from no submission at all, and the reason this is two tables rather than a
    nullable column pair.

    `was_planned` is SNAPSHOTTED onto each child from the code's current
    is_planned, deliberately duplicating reference data. Every code the floor
    uses is unplanned today, but if that ever changes, historical OEE must not
    silently re-rate itself.

    Header and children go in one transaction: a header with a missing child
    would understate downtime and overstate availability, which is worse than
    the write failing outright.

    Codes are validated against the FULL table, retired ones included. Retiring
    a code (docs/sql/13_split_operator_adjustments.sql) is meant to stop NEW
    use, which the form handles by not offering it — but a correction to a
    shift entered before the retirement must be able to carry the old code
    forward, or the newest-header-wins rule would drop those minutes on the
    next save. Only a code that doesn't exist at all is rejected here.
    """
    reasons = get_downtime_reasons(active_only=False)

    unknown = [r.reason_code for r in payload.reasons if r.reason_code not in reasons]
    if unknown:
        raise UnknownReasonCodeError(
            f"Unknown downtime reason code(s): {', '.join(sorted(set(unknown)))}. "
            "Pick one of the codes offered on the form."
        )

    total_minutes = sum(r.minutes for r in payload.reasons)
    if total_minutes > SHIFT_MINUTES:
        raise DowntimeExceedsShiftError(
            f"{total_minutes} minutes of downtime doesn't fit in a "
            f"{SHIFT_MINUTES}-minute shift ({payload.machine_id}, {payload.shift})."
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
    """Appends a scheduling record for one machine+date+shift.

    Only exceptions need writing — absence of a row means scheduled — but a row
    saying scheduled=true is still accepted, since that is how someone undoes a
    not-scheduled mark made by mistake (append-only, so the correction is a new
    row rather than an UPDATE).
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


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

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


def get_latest_downtime_for_date(entry_date: date_type) -> dict[tuple[str, str], dict]:
    """Latest downtime submission per (machine_id, shift) for one date.

    Resolution is per SUBMISSION, not per reason: the newest header for a shift
    wins and older ones are ignored entirely, children and all. That is what
    lets a correction REMOVE a reason entered by mistake — resolving per-reason
    would hit the dead end entries.issue has, where a blank correction can't
    retract an earlier value.

    LEFT JOIN, not JOIN: a header with no children means "ran clean" and has to
    come back as a present-but-empty record. An inner join would drop it and
    make a clean shift indistinguishable from an unentered one.

    Labels come from the FULL code table, retired codes included, so rows
    migrated from the old slot-grain set still resolve to a human name.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (machine_id, shift)
                    id, machine_id, shift, note
                FROM shift_downtime_entry
                WHERE entry_date = %s
                ORDER BY machine_id, shift, created_at DESC
            )
            SELECT l.machine_id, l.shift, l.note,
                   r.reason_code, r.minutes, r.was_planned
            FROM latest l
            LEFT JOIN shift_downtime_reason r ON r.downtime_entry_id = l.id
            ORDER BY l.machine_id, l.shift, r.minutes DESC NULLS LAST
            """,
            (entry_date,),
        ).fetchall()

    labels = get_downtime_reasons(active_only=False)
    downtime: dict[tuple[str, str], dict] = {}

    for machine_id, shift, note, reason_code, minutes, was_planned in rows:
        record = downtime.setdefault(
            (machine_id, shift),
            {"note": note, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0},
        )
        if reason_code is None:
            continue  # header with no children — the "ran clean" case
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


def get_schedule_for_date(entry_date: date_type) -> dict[tuple[str, str], bool]:
    """Latest scheduling record per (machine_id, shift) for one date.

    Only exceptions are stored, so a key missing from this map means the
    machine WAS scheduled. Callers default to True on absence.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, shift)
                machine_id, shift, scheduled
            FROM machine_schedule
            WHERE entry_date = %s
            ORDER BY machine_id, shift, created_at DESC
            """,
            (entry_date,),
        ).fetchall()
    return {(machine_id, shift): scheduled for machine_id, shift, scheduled in rows}


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

    Applies to PRODUCTION UNITS only. Scrap is a single shift total now and has
    nothing to accumulate against.

    A BLANK CHECKPOINT MEANS UNCHANGED, NOT UNKNOWN
    -----------------------------------------------
    Per the floor, a checkpoint is left empty when the number hasn't moved. A
    machine waiting on a delivery, or whose operator got pulled to another line,
    has nothing new to write. So a blank slot produced zero, and a blank FIRST
    slot means the counter was still at zero when the shift started.

    Discarding that is actively harmful, because the blanks are a machine's
    WORST slots. C2 on 2026-09-03 read 10500 / 15750 / 15750 / blank, with
    "operator moved to WS" logged at 12PM:

        blank treated as unknown  ->  15750 / (130 x 360 min) = 33.7%
        blank treated as zero     ->  15750 / (130 x 480 min) = 25.2%

    25.2% is the truth. The earlier reading was too kind, and too kind
    *because* the machine had a bad shift.

    TWO GUARDS
    ----------
    1. A shift with NO readings stays entirely unknown. Silence means nobody
       logged it, not that the machine made nothing for eight hours. A machine
       that genuinely didn't run is recorded with the not-scheduled checkbox.
    2. Slots that have not ELAPSED are unknown, never zero. "Unchanged" is
       meaningless for hours that haven't happened.
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
            deltas[slot] = 0  # carried forward: nothing reported, nothing made
        else:
            deltas[slot] = current - last_known
            last_known = current
    return deltas


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    """Division that returns None instead of raising or defaulting.

    Any unknown input, or a zero denominator, means the ratio is genuinely
    unknowable. Inventing a 0% or 100% for it would be a lie that averages into
    every rollup above it.
    """
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
    """Combines summed components into the four ratios.

    Sums first, divides once — never an average of percentages, so a machine
    that ran 20 minutes cannot weigh the same as one that ran all shift.

    The two ideal figures are accumulated by the CALLER as
    `ideal_per_minute x minutes`, rather than passed as a rate. That is what
    makes this work unchanged for one machine and for a whole zone: 34 machines
    run at 8 different rates, so there is no floor-wide units-per-minute, but
    there is very much a floor-wide count of units it could have made.

    ON THE TWO AVAILABILITIES
    -------------------------
    `availability` is computed on CAPACITY (ideal units at run time over ideal
    units at PPT), not clock minutes. For a single machine the two are
    identical, since its rate cancels. Across machines they diverge, and
    capacity is the correct one: A x P x Q only telescopes back into OEE when
    every factor is weighted the same way, and summing raw minutes treats a
    minute on AS1 (1,680/hr) as worth a minute on C1 (7,800/hr).

    `uptime` keeps the clock-time ratio, because "we ran 91% of the minutes we
    were scheduled for" is a real and useful sentence — it just isn't the OEE
    factor.
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
        # A x P x Q by the long route. Must equal `oee`, which took the short
        # one. Kept in the payload so the identity is checkable rather than
        # merely asserted in a comment.
        "oee_from_factors": (
            availability * performance * quality
            if None not in (availability, performance, quality)
            else None
        ),
    }


def _new_accumulator() -> dict:
    """A zeroed set of the summable components a rollup needs.

    Everything here is additive, which is the point: percentages are never
    combined, only the counts and minutes underneath them, and the division
    happens once at the end in _aggregate().
    """
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
    """Maps an accumulator onto _aggregate's keyword names.

    Exists only so the two rollup calls can't drift over which key feeds which
    argument.
    """
    return {
        "good": acc["good"], "standard": acc["standard"],
        "ppt": acc["ppt"], "run": acc["run"],
        "planned": acc["planned"], "unplanned": acc["unplanned"],
        "ideal_at_ppt": acc["ideal_at_ppt"], "ideal_at_run": acc["ideal_at_run"],
    }


def _build_rollup(machines: dict[str, dict], machine_ids: list[str]) -> dict | None:
    """Combines a set of machines' shift results into one rollup.

    Used for the floor total and for every zone, so there is exactly one
    implementation of the aggregation rules.

    Unscheduled machines are skipped, and so are machines with nothing
    countable: their time isn't production time, and folding either in would
    drag a zone's numbers down over a machine nobody expected to run.

    Two accumulators, because the factors have different populations. OEE and
    Availability need production and downtime, which most machines will have;
    Performance and Quality additionally need scrap. Rolling them together
    would discard a machine's perfectly good OEE because its scrap was missing,
    so both machine counts ship in the result and the page labels a partial
    figure as partial.
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
        # A x P x Q only reconstructs THIS rollup's OEE when all three factors
        # describe the same machines. Performance and Quality come from the
        # scrap-complete subset, so when that is narrower the product mixes two
        # populations and reconstructs nothing. Reported as None rather than
        # shipped as a number that fails its own identity check.
        rollup["oee_from_factors"] = (
            subset["oee_from_factors"]
            if split["machines"] == roll["machines"] else None
        )
        rollup["split_oee"] = subset["oee"]

    return rollup


def _build_pareto(machines: dict[str, dict], machine_ids: list[str]) -> list[dict]:
    """Downtime minutes by reason across a set of machines for one shift.

    Counts every machine that has a downtime submission, whether or not its OEE
    was computable — a reported 20 minutes of material wait is a real 20
    minutes even if the production side is missing. Unscheduled machines are
    excluded, since their downtime isn't production downtime.
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
    """Downtime minutes by reason, biggest first, with each one's share.

    Planned reasons stay in the list even though none of the floor's current
    codes are planned — hiding them would make the unplanned percentages read
    as shares of a total that isn't shown anywhere, and a planned code may be
    added later.
    """
    ranked = sorted(pareto.values(), key=lambda item: item["minutes"], reverse=True)
    unplanned_total = sum(i["minutes"] for i in ranked if not i["is_planned"])
    for item in ranked:
        item["pct_of_unplanned"] = (
            item["minutes"] / unplanned_total
            if unplanned_total and not item["is_planned"] else None
        )
    return ranked


def _machine_warnings(result: dict | None, production_warnings: list[str]) -> list[str]:
    """Warnings worth raising to the top of the page for one machine.

    Soft: they invalidate nothing and the machine still counts. They mean two
    inputs disagree, and the wrong one is usually the machine's configured rate
    rather than what the floor counted.

    Judged on the whole shift, which is now the only grain there is. That was a
    real bug at slot grain: a per-slot ceiling test flagged seven machines that
    were simply running well, because a checkpoint read late borrows units from
    its neighbour. A shift is 480 minutes however the readings fell.
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
    """Reduces a shift's checkpoints to its total, plus any data-quality signal.

    `good` is the sum of the carry-forward deltas, which by construction equals
    the LAST reported reading in the shift. Interior gaps cannot change it,
    which is exactly why shift grain is robust where slot grain was not.

    Two distinct problems, deliberately rated differently:

      units_went_backwards (WARNING) - some checkpoint is lower than the one
          before it. Somebody should fix that reading, but if a LATER reading
          is higher the shift total is still correct, so the machine still
          counts.

      final_reading_low (HARD) - the last reading is below an earlier one, so
          the shift total itself is understated and nothing derived from it can
          be trusted. Excluded.
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
    """Full OEE picture for one production date: every machine, all 3 shifts.

    All three shifts ship in one payload so /oee's shift toggle switches
    client-side with no round trip — the same trade /api/supervisor-data makes,
    affordable for the same reason: this page doesn't poll.

    Machines marked not-scheduled are computed but flagged and left out of the
    rollups entirely. Not 0%, not 100% — absent. A machine with no ideal rate
    seeded gets no OEE at all rather than a guessed ceiling.
    """
    now = now or datetime.now()

    # Imported here rather than at module scope: entries.py owns the production
    # query, and a top-level import would make the two modules mutually
    # importable the moment entries.py ever needs anything from here.
    from app.db.entries import get_latest_entries_for_date

    entries = get_latest_entries_for_date(entry_date)
    units_map = {
        (row["machine_id"], row["time_slot"]): row["units_produced"] for row in entries
    }
    operator_map = {
        (row["machine_id"], row["time_slot"]): row["operator"] for row in entries
    }
    scrap_map = get_latest_scrap_for_date(entry_date)
    downtime_map = get_latest_downtime_for_date(entry_date)
    schedule_map = get_schedule_for_date(entry_date)
    ideal_rates = get_ideal_rates()
    standards = get_shift_standards()

    shifts: dict[str, dict] = {}

    for shift_label in SHIFT_ORDER:
        slots = SHIFT_SLOTS[shift_label]
        elapsed = elapsed_slots(shift_label, entry_date, now)
        elapsed_set = set(elapsed)
        machines: dict[str, dict] = {}

        for machine_id in MACHINE_IDS:
            scheduled = schedule_map.get((machine_id, shift_label), True)
            ideal_per_hour = ideal_rates.get(machine_id)
            downtime = downtime_map.get((machine_id, shift_label))
            scrap = scrap_map.get((machine_id, shift_label))

            readings = {slot: units_map.get((machine_id, slot)) for slot in slots}
            deltas = _cumulative_deltas(readings, slots, elapsed=elapsed_set)
            production = _summarise_production(readings, deltas, elapsed)

            flags = list(production["flags"])
            if ideal_per_hour is None:
                flags.append("no_ideal_rate")

            # Planned Production Time is the elapsed part of the shift. With no
            # planned reason codes in the floor's set, planned downtime is
            # always zero today — but the arithmetic still subtracts it, so
            # adding a planned code later needs no change here.
            planned = downtime["planned_minutes"] if downtime else 0
            unplanned = downtime["unplanned_minutes"] if downtime else 0
            elapsed_minutes = SLOT_MINUTES * len(elapsed)
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

            result = None
            if countable:
                result = _aggregate(
                    good=good,
                    scrap=scrap if scrap_known else None,
                    standard=standards.get((machine_id, shift_label), 0),
                    ppt=ppt,
                    run=run,
                    planned=planned,
                    unplanned=unplanned,
                    ideal_at_ppt=ideal_per_minute * ppt,
                    ideal_at_run=ideal_per_minute * run,
                )

            machines[machine_id] = {
                "scheduled": scheduled,
                "operator": next(
                    (
                        operator_map[(machine_id, slot)]
                        for slot in reversed(slots)
                        if (machine_id, slot) in operator_map
                    ),
                    None,
                ),
                "shift": result,
                # The raw checkpoint sequence, so a flagged machine can be
                # diagnosed from the page rather than from a SQL prompt.
                "checkpoints": [
                    {
                        "slot": slot,
                        "reading": readings[slot],
                        "produced": deltas[slot],
                        "reported": readings[slot] is not None,
                        "elapsed": slot in elapsed_set,
                    }
                    for slot in slots
                ],
                "reasons": downtime["reasons"] if downtime else [],
                "note": downtime["note"] if downtime else None,
                "has_production": good is not None,
                "has_downtime": downtime is not None,
                "has_scrap": scrap is not None,
                "scrap": scrap,
                "scrap_known": scrap_known,
                "elapsed_minutes": elapsed_minutes,
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
            "completeness": _completeness(machines, elapsed),
            "elapsed_slots": elapsed,
        }

    return {
        "date": entry_date.isoformat(),
        "shifts": shifts,
        "shift_minutes": SHIFT_MINUTES,
    }


def _completeness(machines: dict[str, dict], elapsed: list[str]) -> dict:
    """How much of the shift's data has actually been keyed in.

    Counted per COLUMN rather than as one overall percentage. Production comes
    from the 2-hour rounds and OEE data from one person at end of shift, so the
    normal state mid-shift is that the OEE columns are empty and the production
    one isn't. "Machines 34/34, Downtime 12/34" says who to go ask; a single
    "68%" doesn't.

    Production counts a machine once it has ANY reading, because a blank
    checkpoint means unchanged — one reading determines the whole shift.
    Downtime and scrap are single submissions per shift, so they simply exist
    or they don't.
    """
    counts = {"production": 0, "downtime": 0, "scrap": 0}
    total = 0

    for machine in machines.values():
        if not machine["scheduled"]:
            continue
        total += 1
        if not elapsed:
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
        "production": counts["production"],
        "downtime": counts["downtime"],
        "scrap": counts["scrap"],
    }
