"""Data access and OEE computation for /oee.

OEE = Availability x Performance x Quality. This module owns both the reads
and the arithmetic, following the pattern app/db/entries.py set by keeping
compute_status() next to the queries that feed it.

WHY THESE TABLES ARE SEPARATE FROM `entries`
---------------------------------------------
Production, scrap and downtime are entered by DIFFERENT PEOPLE at different
times for the same machine+slot. get_latest_entries_for_date() resolves the
dashboard grid with DISTINCT ON (machine_id, time_slot) ORDER BY created_at
DESC — the newest ROW wins wholesale — so a scrap submission landing on
`entries` would become the newest row for that cell and blank out the units
the production person entered ten minutes earlier. Separate tables let three
people write the same cell concurrently and never collide. See
docs/sql/06_oee_schema.sql.

THE FORMULAS
------------
Per slot (a slot is a 2-hour window; SLOT_MINUTES = 120):

    good        = units_produced[n] - units_produced[n-1]   (cumulative)
    scrap       = scrap_cumulative[n] - scrap_cumulative[n-1]
    total       = good + scrap
    PPT         = SLOT_MINUTES - planned downtime
    run time    = PPT - unplanned downtime

    Availability = run time / PPT
    Performance  = total / (ideal rate x run time)
    Quality      = good / total
    OEE          = A x P x Q

Rolling up across machines, Availability switches from clock minutes to
capacity — ideal units at run time over ideal units at PPT — because 34
machines run at 8 different rates and a minute on AS1 is not worth a minute
on C1. For a single machine the two definitions are the same number. See
_aggregate() for why the distinction is what keeps A x P x Q equal to OEE at
every level of aggregation.

Run time cancels out of that product entirely, which gives a second, simpler
route to the same number:

    OEE = good / (ideal rate x PPT)

Both are computed for every result — `oee` takes the short route,
`oee_from_factors` the long one — and they must agree. The identity is also
why the data requirements are tiered rather than all-or-nothing (next
section).

WHAT EACH NUMBER ACTUALLY NEEDS
-------------------------------
Because scrap cancels out of A x P x Q, OEE does NOT need scrap:

    units + downtime            -> OEE and Availability
    units + downtime + scrap    -> the full A / P / Q split

So a slot missing its scrap still yields a real OEE, with Performance and
Quality reported as N/A rather than guessed. That is deliberate: throwing away
a valid OEE because one of its non-essential inputs is late would be its own
kind of dishonesty.

WHAT IS NEVER DONE
------------------
- No defaulting. A missing downtime submission does NOT mean zero downtime,
  and missing scrap does NOT mean perfect quality. Both yield None, and None
  propagates all the way to an "N/A" on the page. Same rule as a `standards`
  row of 0 (see MACHINE_IDS in app/models.py) one layer up the stack.
- No averaging of percentages. Every rollup sums the underlying counts and
  minutes and divides once, so a machine that ran 20 minutes cannot carry the
  same weight as one that ran all shift.
- No clamping. A Performance over 100% is surfaced, not squashed: it means
  either the ideal rate is set too low for that machine or downtime was
  over-reported, and both are worth seeing.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime

from app.db.postgres import get_pg_connection
from app.models import (
    DASHBOARD_ZONES,
    MACHINE_IDS,
    SHIFT_ORDER,
    SHIFT_SLOTS,
    SLOT_MINUTES,
    DowntimeCreate,
    ScheduleCreate,
    ScrapCreate,
    elapsed_slots,
)


# How far past the derived ceiling a slot has to be before its number is
# treated as broken rather than merely surprising.
#
# The ceiling is standard / 0.75, so it carries real uncertainty — a machine
# whose standard is actually 85% of its maximum will legitimately exceed it.
# Doubling it, though, is not a rate disagreement: it is a transposed digit, or
# a cumulative-for-the-day value in a cumulative-for-the-shift field. Below this
# multiple the slot still counts and merely warns; at or above it, the slot is
# excluded from OEE.
IMPLAUSIBLE_CEILING_MULTIPLE: float = 2.0


class UnknownReasonCodeError(Exception):
    """Raised when a downtime submission names a reason code that isn't in
    (or is no longer active in) the downtime_reasons table.

    A real "we can't classify this" situation: without is_planned we cannot
    tell whether the minutes belong in the OEE denominator, so the submission
    is rejected rather than guessed at.
    """


class DowntimeExceedsSlotError(Exception):
    """Raised when a slot's downtime minutes sum past SLOT_MINUTES.

    A 2-hour slot cannot contain 150 minutes of downtime. Caught at write
    time because the alternative is a negative run time that quietly poisons
    every rollup it touches.
    """


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def get_downtime_reasons(*, active_only: bool = True) -> dict[str, dict]:
    """Reason codes keyed by code, in the order the entry form should show them.

    Python dicts preserve insertion order, so ORDER BY sort_order here is what
    orders the dropdown — the template iterates without re-sorting.
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

    Seeded by docs/sql/08_seed_ideal_rates.sql as standard / 0.75, since a
    standard is 75% of theoretical max. A machine missing from this table
    gets no OEE at all rather than a guessed rate — the same "refuse rather
    than assume" stance get_standard() takes in app/db/entries.py.

    Cast to float on the way out: psycopg hands back Decimal for NUMERIC, and
    these end up in a JSON response where Decimal isn't serialisable. Rates
    are values like 7800.00, so nothing meaningful is lost.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT machine_id, ideal_units_per_hour FROM machine_ideal_rates"
        ).fetchall()
    return {machine_id: float(rate) for machine_id, rate in rows}


def get_slot_standards() -> dict[tuple[str, str], int]:
    """Cumulative standard per (machine_id, time_slot).

    Read whole rather than assuming a flat per-slot increment. The increments
    happen to be uniform within a machine today, but computing per-slot
    targets as cumulative deltas — exactly how units are handled — means a
    machine with an uneven ramp would still get correct per-slot targets.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            "SELECT machine_id, time_slot, standard_units FROM standards"
        ).fetchall()
    return {(machine_id, time_slot): units for machine_id, time_slot, units in rows}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_scrap(payload: ScrapCreate) -> int:
    """Appends a scrap submission. Returns the new row id.

    Append-only like `entries`: a correction is a new row with a later
    created_at, and the reader takes the newest per machine+date+slot.
    """
    with get_pg_connection() as conn:
        row = conn.execute(
            """
            INSERT INTO slot_scrap
                (machine_id, entry_date, time_slot, scrap_cumulative, entered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.entry_date,
                payload.time_slot,
                payload.scrap_cumulative,
                payload.entered_by,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def create_downtime(payload: DowntimeCreate) -> int:
    """Appends a downtime submission (header + one row per reason).

    Returns the new header id. An empty `payload.reasons` writes a header with
    no children, which is how "ran clean, no downtime" is recorded — a
    materially different statement from no submission at all, and the reason
    this is two tables rather than a nullable column pair.

    `was_planned` is SNAPSHOTTED onto each child from the reason code's
    current is_planned, deliberately duplicating reference data. Same
    reasoning as entries.status being stored rather than recomputed: if a code
    is reclassified next year, historical OEE must not silently shift.

    Header and children go in one transaction — a header with a missing child
    would understate that slot's downtime and overstate its availability,
    which is worse than the write failing outright.
    """
    reasons = get_downtime_reasons()

    unknown = [r.reason_code for r in payload.reasons if r.reason_code not in reasons]
    if unknown:
        raise UnknownReasonCodeError(
            f"Unknown or retired downtime reason code(s): {', '.join(sorted(set(unknown)))}. "
            "Pick one of the codes offered on the form."
        )

    total_minutes = sum(r.minutes for r in payload.reasons)
    if total_minutes > SLOT_MINUTES:
        raise DowntimeExceedsSlotError(
            f"{total_minutes} minutes of downtime doesn't fit in a "
            f"{SLOT_MINUTES}-minute slot ({payload.machine_id} {payload.time_slot})."
        )

    with get_pg_connection() as conn:
        header = conn.execute(
            """
            INSERT INTO slot_downtime_entry
                (machine_id, entry_date, time_slot, note, entered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                payload.machine_id,
                payload.entry_date,
                payload.time_slot,
                payload.note,
                payload.entered_by,
            ),
        ).fetchone()
        header_id = header[0]

        for reason in payload.reasons:
            conn.execute(
                """
                INSERT INTO slot_downtime_reason
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

    Only exceptions need writing — absence of a row means scheduled — but a
    row saying scheduled=true is still accepted, since that is how someone
    undoes a not-scheduled mark they made by mistake (append-only, so the
    correction is a new row, not an UPDATE).
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
    """Latest cumulative scrap per (machine_id, time_slot) for one date."""
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (machine_id, time_slot)
                machine_id, time_slot, scrap_cumulative
            FROM slot_scrap
            WHERE entry_date = %s
            ORDER BY machine_id, time_slot, created_at DESC
            """,
            (entry_date,),
        ).fetchall()
    return {(machine_id, time_slot): scrap for machine_id, time_slot, scrap in rows}


def get_latest_downtime_for_date(entry_date: date_type) -> dict[tuple[str, str], dict]:
    """Latest downtime submission per (machine_id, time_slot) for one date.

    Resolution is per SUBMISSION, not per reason: the newest header for a slot
    wins and older headers are ignored entirely, children and all. That is
    what lets a correction REMOVE a reason entered by mistake — resolving
    per-reason would hit the same dead end as entries.issue, where a blank
    correction can't retract an earlier value (see get_shift_activity() in
    app/db/entries.py).

    LEFT JOIN, not JOIN: a header with no children is "ran clean" and has to
    come back as a present-but-empty record. An inner join would drop it and
    make a clean slot indistinguishable from an unentered one.
    """
    with get_pg_connection() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (machine_id, time_slot)
                    id, machine_id, time_slot, note
                FROM slot_downtime_entry
                WHERE entry_date = %s
                ORDER BY machine_id, time_slot, created_at DESC
            )
            SELECT l.machine_id, l.time_slot, l.note,
                   r.reason_code, r.minutes, r.was_planned
            FROM latest l
            LEFT JOIN slot_downtime_reason r ON r.downtime_entry_id = l.id
            ORDER BY l.machine_id, l.time_slot, r.minutes DESC NULLS LAST
            """,
            (entry_date,),
        ).fetchall()

    reason_labels = get_downtime_reasons(active_only=False)
    downtime: dict[tuple[str, str], dict] = {}

    for machine_id, time_slot, note, reason_code, minutes, was_planned in rows:
        key = (machine_id, time_slot)
        record = downtime.setdefault(
            key,
            {"note": note, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0},
        )
        if reason_code is None:
            continue  # header with no children — the "ran clean" case
        record["reasons"].append(
            {
                "code": reason_code,
                "label": reason_labels.get(reason_code, {}).get("label", reason_code),
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
    machine WAS scheduled. Callers should default to True on absence.
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
    """Turns a shift's cumulative checkpoints into per-slot amounts.

    Both units_produced and scrap_cumulative reset at the start of each shift,
    so a slot's amount is the difference from the last reading before it.

    A BLANK CHECKPOINT MEANS UNCHANGED, NOT UNKNOWN
    -----------------------------------------------
    Per the floor, a checkpoint is left empty when the number hasn't moved
    since the last one. A machine waiting on a delivery, or whose operator got
    pulled to another line, has nothing new to write down. So a blank slot
    produced zero, and a blank FIRST slot means the counter was still sitting
    at zero when the shift started.

    That is real information and discarding it is actively harmful, because the
    blanks are a machine's WORST slots. Dropping them as "unknown" removes
    precisely the bad hours from the calculation and flatters the result. Real
    example — C2 on 2026-09-03 read 10500 / 15750 / 15750 / blank, with
    "operator moved to WS" logged at 12PM:

        blank treated as unknown  ->  15750 / (130 x 360 min) = 33.7%
        blank treated as zero     ->  15750 / (130 x 480 min) = 25.2%

    25.2% is the truth. The earlier reading was 8.5 points too kind, and too
    kind *because* the machine had a bad shift.

    It also rescues data that was previously unusable altogether: a shift
    reading blank / 15750 / blank / blank used to yield nothing at all, since
    the 10AM delta had no baseline. Now it is a complete shift: 0, 15750, 0, 0.

    TWO GUARDS
    ----------
    1. A shift with NO readings at all stays entirely unknown. Silence across a
       whole shift means nobody logged it — not that the machine made nothing
       for eight hours. A machine that genuinely didn't run is recorded with
       the not-scheduled checkbox. Without this guard the rule would default
       missing data to a value, which is the `standard_units = 0` mistake
       pointed the other way.

    2. Slots that have not ELAPSED yet are unknown, never zero. "Unchanged"
       is meaningless for two hours that haven't happened, and without this a
       machine viewed mid-shift would be charged for the rest of its day.
       Callers pass `elapsed` for live data; `standards` omits it, since a
       target exists whether or not the clock has reached it.
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
            # Carried forward: nothing was reported, so nothing was produced.
            deltas[slot] = 0
        else:
            deltas[slot] = current - last_known
            last_known = current
    return deltas


def _safe_divide(numerator: float | None, denominator: float | None) -> float | None:
    """Division that returns None instead of raising or defaulting.

    Any unknown input, or a zero denominator, means the ratio is genuinely
    unknowable — a slot entirely consumed by planned downtime has no
    Availability to speak of, and inventing 0% or 100% for it would be a lie
    that averages into every rollup above it.
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _compute_slot(
    *,
    good: int | None,
    scrap: int | None,
    standard: int | None,
    downtime: dict | None,
    ideal_per_minute: float,
    is_elapsed: bool,
    units_reported: bool = True,
    scrap_reported: bool = True,
) -> dict:
    """Everything derivable about one machine-slot.

    `counted` is the gate for whether this slot feeds the shift rollup. It
    requires a computable units delta and a downtime submission, and it
    requires the numbers to be physically possible — a slot carrying a flag is
    reported but never summed, so one transposed digit can't move a whole
    zone's OEE.
    """
    has_units = good is not None
    has_scrap = scrap is not None
    has_downtime = downtime is not None

    planned = downtime["planned_minutes"] if has_downtime else None
    unplanned = downtime["unplanned_minutes"] if has_downtime else None
    ppt = SLOT_MINUTES - planned if has_downtime else None
    run = ppt - unplanned if has_downtime else None

    total = good + scrap if has_units and has_scrap else None
    ideal_slot_units = ideal_per_minute * SLOT_MINUTES

    # Impossible-number checks. These matter more than missing data does: a
    # blank cell is visible and someone chases it, whereas a negative delta or
    # a transposed digit produces a plausible-looking wrong OEE that nobody
    # questions.
    flags: list[str] = []
    if has_units and good < 0:
        # Cumulative counters cannot go backwards. Means a typo, or a per-slot
        # number typed into the cumulative field.
        flags.append("negative_units")
    if has_scrap and scrap < 0:
        flags.append("negative_scrap")
    if has_units and good > ideal_slot_units * IMPLAUSIBLE_CEILING_MULTIPLE:
        # Comfortably past even a generous reading of the ceiling. At more than
        # double the theoretical maximum this is a transposed digit or a value
        # typed as cumulative-for-the-DAY rather than for the shift, not a
        # disagreement about the machine's rate.
        flags.append("implausible_units")
    if run is not None and run < 0:
        flags.append("downtime_over_slot")

    counted = has_units and has_downtime and not flags

    # Soft warnings: surfaced, but they do NOT exclude the slot, because the
    # numbers are individually possible and it's their COMBINATION that's
    # suspect. Nothing is clamped — an OEE of 129% is shown as 129%, with the
    # reason attached, rather than quietly rounded down to 100% and forgotten.
    oee_value = _safe_divide(good, ideal_per_minute * ppt) if ppt else None
    warnings: list[str] = []

    # A single slot over its ceiling is recorded here for the cell's tooltip,
    # but it is NOT raised to the page banner — see _machine_warnings(), which
    # judges this at shift level instead. Per-slot deltas carry checkpoint
    # timing noise that a shift total does not.
    if oee_value is not None and oee_value > 1:
        warnings.append("over_100")

    return {
        "good": good,
        "scrap": scrap,
        "total": total,
        "standard": standard,
        "planned_minutes": planned,
        "unplanned_minutes": unplanned,
        "ppt_minutes": ppt,
        "run_minutes": run,
        "ideal_units": ideal_slot_units,
        # For a single machine-slot the capacity-weighted and clock-time
        # availabilities are the same number (the rate cancels), so both keys
        # carry it — the shift and rollup levels need them distinguished, and
        # a consistent shape keeps the front end from special-casing depth.
        "availability": _safe_divide(run, ppt),
        "uptime": _safe_divide(run, ppt),
        "performance": _safe_divide(total, ideal_per_minute * run) if run else None,
        "quality": _safe_divide(good, total),
        "oee": _safe_divide(good, ideal_per_minute * ppt) if ppt else None,
        "pct_of_standard": _safe_divide(good, standard),
        "reasons": downtime["reasons"] if has_downtime else [],
        "note": downtime["note"] if has_downtime else None,
        "has_units": has_units,
        "has_scrap": has_scrap,
        "has_downtime": has_downtime,
        # False when the value is known only by carry-forward — nobody typed a
        # number for this slot because it hadn't moved. Display-only.
        "units_reported": units_reported,
        "scrap_reported": scrap_reported,
        "is_elapsed": is_elapsed,
        "counted": counted,
        "flags": flags,
        "warnings": warnings,
    }


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
    """Rolls summed components up into the four ratios.

    Sums first, divides once — never an average of percentages, so a machine
    that ran 20 minutes cannot carry the same weight as one that ran all
    shift.

    The two ideal figures are accumulated by the CALLER as
    `ideal_per_minute x minutes` per slot, rather than passed as a single rate.
    That is what makes this function work unchanged for one machine and for a
    whole zone: 34 machines run at 8 different rates, so there is no such
    thing as a floor-wide units-per-minute, but there is very much such a
    thing as a floor-wide count of units it could have made.

    `scrap` is None when any contributing slot was missing its scrap, in which
    case Performance and Quality come back None while OEE and Availability,
    which don't need scrap, still resolve.

    ON THE TWO AVAILABILITIES
    -------------------------
    `availability` is computed on CAPACITY (ideal units at run time over ideal
    units at PPT), not on raw clock minutes. For a single machine the two are
    arithmetically identical, since its rate cancels: (rate x run) / (rate x
    ppt) == run / ppt. Across machines they diverge, and capacity is the one
    that's correct.

    The reason is that A x P x Q only telescopes back into OEE when every
    factor is weighted the same way. Summing raw minutes across the floor
    treats a minute on AS1 (1,680/hr) as worth a minute on C1 (7,800/hr),
    while OEE quite rightly does not — so a clock-time availability makes the
    zone rollup's three factors multiply out to something that isn't its OEE.
    Weighting by capacity fixes it at every level of aggregation.

    `uptime` keeps the clock-time ratio, because "we ran 91% of the minutes we
    were scheduled for" is a real and useful sentence — it just isn't the OEE
    factor. Expect the two to differ on a rollup and to match exactly on a
    single machine.
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
        # Carried explicitly rather than left for the caller to back out of
        # ppt. Reconstructing planned downtime needs the number of counted
        # slots as well (planned = 120 x slots - ppt), and a front end that
        # assumed a full four-slot shift would misreport every partial one.
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
        # A x P x Q by the long route. Must equal `oee` above, which took the
        # short one (run time cancels out of the product). Kept in the payload
        # so the identity is checkable from a test or by eye rather than
        # merely asserted in a comment.
        "oee_from_factors": (
            availability * performance * quality
            if None not in (availability, performance, quality)
            else None
        ),
    }


def _machine_warnings(
    machine_shift: dict | None, *, slots_counted: int, slots_in_shift: int
) -> list[str]:
    """Warnings worth raising to the top of the page for one machine.

    Judged on the SHIFT aggregate, deliberately, not on individual slots.

    A slot's delta is the gap between two hand-taken checkpoints, and those
    readings are not taken exactly on the slot boundary — a reading logged late
    pushes the next window's units into this one. Across a shift that cancels
    out; within a slot it swings hard. Real numbers from the first day of use:

        WS1   8AM 14225   10AM 7538   12PM 13438   2PM 12442
        C8    8AM 19500   10AM    0   12PM  6000   2PM   600

    Both machines finished the shift comfortably UNDER their ceiling (90% and
    45% of it), yet a per-slot test flagged them, along with five others — six
    of the seven on the 8AM slot, the first of the shift. That test was
    measuring how punctually the checkpoints were read, not what the machines
    were capable of, and it put an alarming banner on a completely ordinary
    day.

    The shift total has no such problem: four slots are 480 minutes no matter
    when each reading was taken. So the ceiling gets compared there, where
    exceeding it is a real statement about the machine's configured rate.

    That only holds while the "shift" actually covers most of a shift, though.
    AS4 reported 8AM and 12PM with 10AM missing, which leaves ONE countable
    slot — and a one-slot aggregate is just a slot delta again, carrying all
    the timing noise this function exists to see past. So a majority of the
    shift's slots have to be counted before the comparison is allowed to speak.
    Below that the machine simply reports its OEE with a low slot count, which
    is the honest signal, rather than an accusation built on one reading.
    """
    if machine_shift is None:
        return []

    warnings: list[str] = []
    oee = machine_shift.get("oee")
    covers_most_of_shift = slots_counted * 2 > slots_in_shift
    if oee is not None and oee > 1 and covers_most_of_shift:
        warnings.append("over_100")
    return warnings


def _new_accumulator() -> dict:
    """A zeroed set of the summable components a rollup needs.

    Everything here is additive, which is the whole point: percentages are
    never combined, only the counts and minutes underneath them, and the
    division happens once at the end in _aggregate().
    """
    return {
        "good": 0,
        "scrap": 0,
        "standard": 0,
        "ppt": 0.0,
        "run": 0.0,
        "planned": 0.0,
        "unplanned": 0.0,
        "ideal_at_ppt": 0.0,
        "ideal_at_run": 0.0,
        "machines": 0,
    }


def _accumulate(acc: dict, machine_shift: dict, *, with_scrap: bool = False) -> None:
    """Folds one machine's shift result into a rollup accumulator."""
    acc["good"] += machine_shift["good"]
    acc["standard"] += machine_shift["standard"]
    acc["ppt"] += machine_shift["ppt_minutes"]
    acc["run"] += machine_shift["run_minutes"]
    acc["planned"] += machine_shift["planned_minutes"]
    acc["unplanned"] += machine_shift["unplanned_minutes"]
    acc["ideal_at_ppt"] += machine_shift["ideal_at_ppt"]
    acc["ideal_at_run"] += machine_shift["ideal_at_run"]
    acc["machines"] += 1
    if with_scrap:
        acc["scrap"] += machine_shift["scrap"]


def _accumulator_args(acc: dict) -> dict:
    """Maps an accumulator onto _aggregate's keyword names.

    Exists only so the two rollup calls can't drift out of step with each
    other over which key feeds which argument.
    """
    return {
        "good": acc["good"],
        "standard": acc["standard"],
        "ppt": acc["ppt"],
        "run": acc["run"],
        "planned": acc["planned"],
        "unplanned": acc["unplanned"],
        "ideal_at_ppt": acc["ideal_at_ppt"],
        "ideal_at_run": acc["ideal_at_run"],
    }


def compute_oee_report(entry_date: date_type, *, now: datetime | None = None) -> dict:
    """Full OEE picture for one production date: every machine, all 3 shifts.

    Returns all three shifts in one payload so /oee's shift toggle switches
    client-side with no round trip — the same trade /api/supervisor-data makes,
    and affordable for the same reason: this page doesn't poll.

    Machines marked not-scheduled for a shift are computed but flagged
    `scheduled: false` and left out of the rollup entirely. Not 0%, not 100% —
    absent. A machine with no ideal rate seeded gets no OEE at all, rather
    than a guessed ceiling.
    """
    now = now or datetime.now()

    # Imported here rather than at module scope: entries.py is the owner of
    # the production-side query and importing it at the top would make the two
    # modules mutually importable the moment entries.py ever needs anything
    # from here.
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
    standards = get_slot_standards()

    shifts: dict[str, dict] = {}

    for shift_label in SHIFT_ORDER:
        slots = SHIFT_SLOTS[shift_label]
        elapsed = set(elapsed_slots(shift_label, entry_date, now))

        machines: dict[str, dict] = {}

        for machine_id in MACHINE_IDS:
            ideal_per_hour = ideal_rates.get(machine_id)
            scheduled = schedule_map.get((machine_id, shift_label), True)

            if ideal_per_hour is None:
                machines[machine_id] = {
                    "scheduled": scheduled,
                    "slots": {},
                    "shift": None,
                    "slots_counted": 0,
                    "slots_elapsed": len(elapsed),
                    "scrap_complete": False,
                    "flags": ["no_ideal_rate"],
                    "warnings": [],
                }
                continue

            ideal_per_minute = ideal_per_hour / 60

            raw_units = {slot: units_map.get((machine_id, slot)) for slot in slots}
            raw_scrap = {slot: scrap_map.get((machine_id, slot)) for slot in slots}

            unit_deltas = _cumulative_deltas(raw_units, slots, elapsed=elapsed)
            scrap_deltas = _cumulative_deltas(raw_scrap, slots, elapsed=elapsed)
            # Standards are fully seeded for all 12 slots, so carry-forward
            # never fires here; `elapsed` is omitted because a target exists
            # whether or not the clock has reached that slot yet.
            standard_deltas = _cumulative_deltas(
                {slot: standards.get((machine_id, slot)) for slot in slots}, slots
            )

            slot_results: dict[str, dict] = {}
            for slot in slots:
                slot_results[slot] = _compute_slot(
                    good=unit_deltas[slot],
                    scrap=scrap_deltas[slot],
                    standard=standard_deltas[slot],
                    downtime=downtime_map.get((machine_id, slot)),
                    ideal_per_minute=ideal_per_minute,
                    is_elapsed=slot in elapsed,
                    # Whether a number was actually keyed in for this slot, as
                    # opposed to its value being known by carry-forward. The
                    # maths doesn't care — both are equally known — but the
                    # tooltip should say which, so nobody mistakes an inferred
                    # zero for a typed one.
                    units_reported=raw_units[slot] is not None,
                    scrap_reported=raw_scrap[slot] is not None,
                )

            # A slot whose BASELINE is known-bad is measuring from a wrong
            # number, so its own delta is meaningless even though it looks
            # fine in isolation.
            #
            # The classic shape: 12PM gets typed low, so 12PM's delta comes out
            # negative (caught) and 2PM's delta comes out huge — not because
            # anything was produced, but because the counter is recovering the
            # ground the typo lost. Counting that inflates the whole shift.
            #
            # This is the same rule _cumulative_deltas() already applies to a
            # MISSING baseline; a known-wrong baseline deserves it just as
            # much. One bad checkpoint poisons exactly two deltas: its own and
            # the next.
            for index, slot in enumerate(slots):
                if index == 0:
                    continue
                previous = slot_results[slots[index - 1]]
                if {"negative_units", "implausible_units"} & set(previous["flags"]):
                    current = slot_results[slot]
                    if "baseline_suspect" not in current["flags"]:
                        current["flags"].append("baseline_suspect")
                    current["counted"] = False

            counted = [s for s in slot_results.values() if s["counted"]]
            scrap_complete = bool(counted) and all(s["has_scrap"] for s in counted)

            machine_shift = None
            if counted:
                machine_shift = _aggregate(
                    good=sum(s["good"] for s in counted),
                    scrap=sum(s["scrap"] for s in counted) if scrap_complete else None,
                    standard=sum(s["standard"] or 0 for s in counted),
                    ppt=sum(s["ppt_minutes"] for s in counted),
                    run=sum(s["run_minutes"] for s in counted),
                    planned=sum(s["planned_minutes"] for s in counted),
                    unplanned=sum(s["unplanned_minutes"] for s in counted),
                    ideal_at_ppt=sum(
                        ideal_per_minute * s["ppt_minutes"] for s in counted
                    ),
                    ideal_at_run=sum(
                        ideal_per_minute * s["run_minutes"] for s in counted
                    ),
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
                "slots": slot_results,
                "shift": machine_shift,
                "slots_counted": len(counted),
                "slots_elapsed": len(elapsed),
                "scrap_complete": scrap_complete,
                "flags": sorted({flag for s in slot_results.values() for flag in s["flags"]}),
                "warnings": _machine_warnings(
                    machine_shift,
                    slots_counted=len(counted),
                    slots_in_shift=len(slots),
                ),
            }

        # Rollups are built here rather than accumulated inside the loop above,
        # so the same function produces the floor total and every zone total.
        # That matters more than the tidiness: the capacity-weighted
        # availability in _aggregate() is subtle enough that a second
        # implementation of it — in the front end, say — would drift.
        shifts[shift_label] = {
            "machines": machines,
            "rollup": _build_rollup(machines, MACHINE_IDS),
            "zones": {
                slug: _build_rollup(machines, zone_machines)
                for slug, zone_machines in DASHBOARD_ZONES.items()
            },
            "pareto": _build_pareto(machines, MACHINE_IDS, slots),
            "completeness": _completeness(machines, slots, elapsed),
            "elapsed_slots": sorted(elapsed, key=slots.index),
        }

    return {
        "date": entry_date.isoformat(),
        "shifts": shifts,
        "slot_minutes": SLOT_MINUTES,
    }


def _build_rollup(machines: dict[str, dict], machine_ids: list[str]) -> dict | None:
    """Combines a set of machines' shift results into one rollup.

    Used for both the floor total and each zone's total, so there is exactly
    one implementation of the aggregation rules.

    Unscheduled machines are skipped, and so are machines with nothing
    countable: their time isn't production time, and folding either in would
    drag a zone's numbers down over a machine nobody expected to run.

    Two accumulators, not one, because the rollup's factors have different
    populations. OEE and Availability need units and downtime, which most
    machines will have; Performance and Quality additionally need scrap, which
    mid-shift often lags. Rolling them together would mean discarding a
    machine's perfectly good OEE just because its scrap hadn't been keyed in
    yet, so both machine counts ship in the result and the page can label a
    partial figure as partial.
    """
    roll = _new_accumulator()
    split = _new_accumulator()

    for machine_id in machine_ids:
        machine = machines.get(machine_id)
        if machine is None or not machine["scheduled"] or machine["shift"] is None:
            continue
        _accumulate(roll, machine["shift"])
        if machine["scrap_complete"]:
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
        # describe the same set of machines. Performance and Quality above come
        # from the scrap-complete subset, so whenever that subset is narrower
        # than the OEE population the product mixes two populations and
        # reconstructs nothing. Reported as None in that case rather than
        # shipped as a number that quietly fails its own identity check.
        rollup["oee_from_factors"] = (
            subset["oee_from_factors"]
            if split["machines"] == roll["machines"]
            else None
        )
        # The subset's own internally-consistent OEE, so a caller wanting a
        # fully-decomposed number has one, alongside the count it covers.
        rollup["split_oee"] = subset["oee"]

    return rollup


def _build_pareto(
    machines: dict[str, dict], machine_ids: list[str], slots: list[str]
) -> list[dict]:
    """Downtime minutes by reason across a set of machines for one shift.

    Counts every slot that has a downtime submission, whether or not that slot
    counted toward OEE — a reported 20 minutes of material wait is a real 20
    minutes even if the slot's units were missing and its OEE unknowable.
    Unscheduled machines are still excluded, since their downtime isn't
    production downtime.
    """
    pareto: dict[str, dict] = {}
    for machine_id in machine_ids:
        machine = machines.get(machine_id)
        if machine is None or not machine["scheduled"]:
            continue
        for slot in slots:
            slot_result = machine["slots"].get(slot)
            if not slot_result:
                continue
            for reason in slot_result["reasons"]:
                bucket = pareto.setdefault(
                    reason["code"],
                    {
                        "code": reason["code"],
                        "label": reason["label"],
                        "is_planned": reason["is_planned"],
                        "minutes": 0,
                        "occurrences": 0,
                    },
                )
                bucket["minutes"] += reason["minutes"]
                bucket["occurrences"] += 1
    return _rank_pareto(pareto)


def _rank_pareto(pareto: dict[str, dict]) -> list[dict]:
    """Downtime minutes by reason, biggest first, with each one's share.

    Unplanned reasons are what the page leads with, but planned ones stay in
    the list — a shift dominated by planned maintenance is worth seeing, and
    hiding it would make the unplanned percentages read as shares of a total
    that isn't shown anywhere.
    """
    ranked = sorted(pareto.values(), key=lambda item: item["minutes"], reverse=True)
    unplanned_total = sum(i["minutes"] for i in ranked if not i["is_planned"])
    for item in ranked:
        item["pct_of_unplanned"] = (
            item["minutes"] / unplanned_total
            if unplanned_total and not item["is_planned"]
            else None
        )
    return ranked


def _completeness(machines: dict[str, dict], slots: list[str], elapsed: set[str]) -> dict:
    """How much of the shift's data has actually been keyed in.

    Counted per COLUMN rather than as one overall percentage, because with
    three different people entering three different things the normal state
    mid-shift is that one of them is behind. "units 34/34, scrap 31/34,
    downtime 28/34" tells a supervisor who to go ask; a single "91%" doesn't.

    Only elapsed slots count toward the denominator, so the rest of today
    isn't reported as missing.
    """
    expected = len(elapsed)
    counts = {"units": 0, "scrap": 0, "downtime": 0}
    machine_total = 0

    for machine in machines.values():
        if not machine["scheduled"]:
            continue
        machine_total += 1
        if not expected:
            continue
        for field, key in (("units", "has_units"), ("scrap", "has_scrap"), ("downtime", "has_downtime")):
            if all(machine["slots"].get(slot, {}).get(key) for slot in elapsed):
                counts[field] += 1

    return {
        "machines": machine_total,
        "slots_expected": expected,
        "units": counts["units"],
        "scrap": counts["scrap"],
        "downtime": counts["downtime"],
    }
