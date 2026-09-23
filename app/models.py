"""Reference lists, shift geometry and request models."""
from datetime import date as date_type
from datetime import datetime, timedelta

from pydantic import BaseModel, Field, field_validator

# The 12 checkpoints of the day, in display order. They must match the slot
# values in the `standards` table.
TIME_SLOTS: list[str] = [
    "8AM", "10AM", "12PM", "2PM", "4PM", "6PM",
    "8PM", "10PM", "12AM", "2AM", "4AM", "6AM",
]

# Full machine roster. Every machine here needs real rows in `standards`:
# a missing row raises StandardNotFoundError on entry, and a row of 0 would
# make the machine's cells permanently green (status is units >= standard).
MACHINE_IDS: list[str] = [
    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11",
    "C14", "C15", "C16",
    "FM1", "FM2", "FM3",
    "WS1", "WS2", "WS3", "WS4", "WS5", "WS6",
    "P1", "P2", "P3", "P4",
    "AS1", "AS2", "AS3", "AS4", "AS5", "AS6", "AS7",
]

# One floor screen per zone, served at /dashboard/<slug>.
DASHBOARD_ZONES: dict[str, list[str]] = {
    "b2": ["FM1", "FM2", "FM3"],
    "b3": ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C14", "C15", "C16"],
    "b4": ["P1", "P2", "P3", "P4"],
    "ws": ["WS1", "WS2", "WS3", "WS4", "WS5", "WS6"],
    "leno": ["AS1", "AS2", "AS3", "AS4", "AS5", "AS6", "AS7"],
}

DASHBOARD_ZONE_LABELS: dict[str, str] = {
    "b2": "FM",
    "b3": "Combo/FMW",
    "b4": "Poly",
    "ws": "WS",
    "leno": "Leno",
}

# Top-to-bottom zone order on /supervisor. Slugs missing from DASHBOARD_ZONES
# are skipped, so retiring a zone can't break the page.
SUPERVISOR_ZONE_ORDER: list[str] = ["b3", "b2", "ws", "b4", "leno"]

# Three 8-hour shifts, keyed by the server-local hour each one starts.
SHIFTS: list[tuple[int, str]] = [
    (6, "1st Shift"),   # 6AM - 2PM
    (14, "2nd Shift"),  # 2PM - 10PM
    (22, "3rd Shift"),  # 10PM - 6AM
]

# The dashboard keeps showing the outgoing shift for this many hours after a
# changeover, so the crew can see their final numbers. With 1, the board
# switches to 2nd Shift at 3PM rather than 2PM.
SHIFT_DISPLAY_DELAY_HOURS: int = 1

# The four checkpoints each shift owns. The boards and the entry form show
# only the current shift's four.
SHIFT_SLOTS: dict[str, list[str]] = {
    "1st Shift": ["8AM", "10AM", "12PM", "2PM"],
    "2nd Shift": ["4PM", "6PM", "8PM", "10PM"],
    "3rd Shift": ["12AM", "2AM", "4AM", "6AM"],
}

SHIFT_ORDER: list[str] = [label for _, label in SHIFTS]

# /supervisor's extra option that shows all 12 columns. Kept out of
# SHIFT_ORDER and SHIFT_SLOTS because it isn't a shift and must never be
# saved onto an entry.
ALL_DAY_LABEL: str = "All Day"


def get_current_shift(now: datetime) -> str:
    """The shift the dashboard should display at `now` (server-local time).

    Each shift's start is pushed back by SHIFT_DISPLAY_DELAY_HOURS. Hours
    before the first boundary fall through to the last shift, which wraps
    past midnight.
    """
    current = SHIFTS[-1][1]
    for start_hour, label in SHIFTS:
        display_hour = (start_hour + SHIFT_DISPLAY_DELAY_HOURS) % 24
        if now.hour >= display_hour:
            current = label
    return current


def resolve_shift(requested: str | None, now: datetime) -> str:
    """Which shift a /console/<zone> page is logging against.

    `requested` comes from the page's shift toggle. Anything unrecognised
    falls back to get_current_shift().

    This is separate from the dashboard's rule on purpose: the board rolls to
    2nd Shift at 3PM, but people are often still entering 1st Shift numbers
    until 4PM, so the form must not follow the display delay.
    """
    if requested in SHIFT_SLOTS:
        return requested
    return get_current_shift(now)


class EntryCreate(BaseModel):
    """Payload accepted from the /console submission form."""

    machine_id: str
    operator: str = Field(min_length=1, max_length=100)
    time_slot: str
    units_produced: int = Field(ge=0)
    issue: str | None = Field(default=None, max_length=500)
    entered_by: str = Field(min_length=1, max_length=20)
    entry_date: date_type


class Entry(EntryCreate):
    """A stored entry, including server-computed fields."""

    id: int
    status: str  # ":)" or ":("


# ---------------------------------------------------------------------------
# OEE: reference values and slot geometry
#
# OEE = Availability x Performance x Quality, computed in app/db/oee.py.
# ---------------------------------------------------------------------------

# Each slot is the END of a 2-hour window (8AM covers 6-8AM), so four slots
# make one 8-hour shift. Machines don't stop for breaks (someone covers), so
# there's no break allowance to subtract.
SLOT_MINUTES: int = 120

# One 8-hour shift: 480 minutes.
SHIFT_MINUTES: int = SLOT_MINUTES * len(SHIFT_SLOTS[SHIFT_ORDER[0]])

# Standards are set at 75% of each machine's theoretical maximum. Kept here
# for reference only. The OEE math reads each machine's ideal rate from the
# `machine_ideal_rates` table, so changing a target can't rewrite history.
STANDARD_PCT_OF_IDEAL: float = 0.75


def _slot_end_hour(slot: str) -> int:
    """Slot label to the 24h hour its window closes on.

    "8AM" -> 8, "2PM" -> 14, "12AM" -> 0, "12PM" -> 12.
    """
    meridiem = slot[-2:]
    hour = int(slot[:-2])
    if meridiem == "AM":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


SLOT_END_HOURS: dict[str, int] = {slot: _slot_end_hour(slot) for slot in TIME_SLOTS}

# Each slot's shift and 1-based position in it. Counters reset every shift,
# so slot 1's delta is its own value and slot n's is the gap from slot n-1.
SLOT_POSITIONS: dict[str, tuple[str, int]] = {
    slot: (label, index)
    for label, slots in SHIFT_SLOTS.items()
    for index, slot in enumerate(slots, start=1)
}

OEE_ZONE_ORDER: list[str] = SUPERVISOR_ZONE_ORDER

# Longer periods the /oee downtime Pareto can show besides one shift: rolling
# windows ending on (and including) the selected date, all shifts combined.
PARETO_RANGE_DAYS: list[int] = [7, 30]


# ---------------------------------------------------------------------------
# Shift length (OEE only)
#
# A machine can be run by one crew for 10 or 12 hours instead of the usual
# 8. The 2-hour rounds and the floor screens stay on the 8-hour rotation (the
# crew keeps writing the running count into the 4PM and 6PM boxes), but OEE
# has to judge the machine against the minutes it actually ran.
#
# Rules:
#   - The production day always starts at 6AM.
#   - A shift's length decides where it starts: the Nth L-hour shift starts
#     at 6AM + N x L. So a 12h 2nd Shift is 6PM-6AM, a 10h one 4PM-2AM, an
#     8h one 2PM-10PM.
#   - Each shift is set per machine per day. C1 can run an 8h 1st Shift, sit
#     idle 2PM-6PM, then have a 12h crew from 6PM (1st = 8, 2nd = 12).
#   - A later shift can be as long or longer than the one before it, never
#     shorter; a shorter one would start inside it and count slots twice.
#     Valid days: 8/8/8, 8/10, 8/12, 10/10, 10/12, 12/12. A 3rd Shift only
#     exists on an all-8h day (or a short day, below).
#   - A shift with no length set inherits the one before it (the 1st
#     defaults to 8).
#
# Short days
# ----------
# Some days (mostly Saturdays) run on 6-hour shifts: 1st 6AM-12PM, 2nd
# 12PM-6PM, 3rd 6PM-12AM, nothing 12AM-6AM. The stored length is a whole
# number of hours, 1 to 6, and it means two things:
#
#   - The shift sits in the 6-hour pattern (pattern_hours() returns 6). Every
#     rule above works on the pattern: where the shift starts, which
#     checkpoints it owns, and the as-long-or-longer check.
#   - The machine was scheduled for that many hours of it, from the shift's
#     start. That's Planned Production Time (hours x 60), and the standard
#     scales with it. 4 on the 1st Shift means 6AM-10AM, 240 minutes.
#
# The hours are scheduled time, not time run. A 6-hour shift where the
# operator missed two hours is 6 with 120 minutes of Lack of Operator;
# entering 4 would hide that loss.
#
# Production is still the last reading in the shift's own checkpoints, and
# each 6-hour crew starts counting from zero. A shift after a short one
# inherits the full 6-hour pattern, and a short day's 2nd and 3rd Shifts
# default to not scheduled (see default_scheduled()).
#
# Lengths are stored per (machine, production day, shift) in
# `machine_shift_length`; no row means inherit.
#
# The production day
# ------------------
# Review pages (/oee, /supervisor, /console/oee) date a shift by the day it
# STARTED: "3rd Shift, Thursday" is Thursday 10PM to Friday 6AM. The 2-hour
# rounds and the floor screens file 12AM-6AM checkpoints under the morning
# they land on, and `entries` keeps that convention. shift_slot_dates() maps
# between the two by returning each checkpoint with the calendar date it was
# filed under.
# ---------------------------------------------------------------------------

SHIFT_LENGTH_HOURS: list[int] = [8, 10, 12]
DEFAULT_SHIFT_HOURS: int = 8

SHORT_PATTERN_HOURS: int = 6
SHORT_SHIFT_HOURS: list[int] = list(range(1, SHORT_PATTERN_HOURS + 1))

# Every value machine_shift_length can hold. Must match its CHECK constraint.
VALID_SHIFT_HOURS: list[int] = SHORT_SHIFT_HOURS + SHIFT_LENGTH_HOURS

# Upper bound on one downtime reason's minutes, matching the CHECK on
# shift_downtime_reason. create_shift_downtime() applies the tighter cap of
# the machine's actual shift length.
MAX_SHIFT_MINUTES: int = max(SHIFT_LENGTH_HOURS) * 60

DAY_START_HOUR: int = SHIFTS[0][0]

# Slots closing at or before the day start (12AM, 2AM, 4AM, 6AM). They
# belong to the production day that started the evening before, but the
# rounds file them under the next calendar date.
NIGHT_SLOTS: list[str] = [
    slot for slot in TIME_SLOTS if SLOT_END_HOURS[slot] <= DAY_START_HOUR
]


def pattern_hours(shift_hours: int) -> int:
    """The shift pattern a stored length belongs to: 6 for a short day's 1-6,
    otherwise the length itself."""
    return SHORT_PATTERN_HOURS if shift_hours <= SHORT_PATTERN_HOURS else shift_hours


def shift_slots(shift: str, shift_hours: int) -> list[str] | None:
    """The checkpoints of `shift` at `shift_hours` long, or None if the day
    has no room for it.

    The Nth L-hour shift is the Nth chunk of L/2 slots out of TIME_SLOTS:

        1st, 8h  -> 8AM..2PM      2nd, 8h  -> 4PM..10PM    3rd, 8h -> 12AM..6AM
        1st, 10h -> 8AM..4PM      2nd, 10h -> 6PM..2AM     3rd, 10h: none
        1st, 12h -> 8AM..6PM      2nd, 12h -> 8PM..6AM     3rd, 12h: none
        1st, 1-6 -> 8AM..12PM     2nd, 1-6 -> 2PM..6PM     3rd, 1-6 -> 8PM..12AM

    A short-day shift owns its whole 6-hour window whatever hours were
    entered, so it doesn't matter which of its boxes holds the final count.
    """
    per_shift = pattern_hours(shift_hours) * 60 // SLOT_MINUTES
    index = SHIFT_ORDER.index(shift)
    chunk = TIME_SLOTS[index * per_shift:(index + 1) * per_shift]
    return chunk if len(chunk) == per_shift else None


def shift_plan(shift_hours: int) -> dict[str, list[str]]:
    """Every shift's slots on a day where all shifts are `shift_hours` long.
    The 8-hour plan must equal SHIFT_SLOTS (asserted at the bottom of this
    file)."""
    plan: dict[str, list[str]] = {}
    for label in SHIFT_ORDER:
        slots = shift_slots(label, shift_hours)
        if slots is not None:
            plan[label] = slots
    return plan


def resolve_day_lengths(explicit: dict[str, int]) -> dict[str, int]:
    """Each shift's length for one machine on one day, given what was set.

    The 1st Shift defaults to 8 and each later shift inherits the pattern of
    the one before it. After a 4-hour short 1st Shift, the 2nd is a full 6.

    Returns a length for all three shifts even when one can't exist at that
    length; shift_slots() is the existence test.
    """
    lengths: dict[str, int] = {}
    previous = DEFAULT_SHIFT_HOURS
    for label in SHIFT_ORDER:
        previous = explicit.get(label, pattern_hours(previous))
        lengths[label] = previous
    return lengths


def shift_length_conflict(lengths: dict[str, int]) -> str | None:
    """A message describing the first overlap on the day, or None.

    Compared on the pattern, so short-day shifts are all 6 here: 4 then 6 is
    fine, but a short shift after an 8-hour one would start inside it.
    """
    for previous, current in zip(SHIFT_ORDER, SHIFT_ORDER[1:]):
        if pattern_hours(lengths[current]) < pattern_hours(lengths[previous]):
            short = lengths[current] <= SHORT_PATTERN_HOURS
            return (
                f"{current} ({lengths[current]}h, {shift_span(current, lengths[current])}) "
                + (f"can't be a short-day shift after {previous}" if short
                   else f"can't be shorter than {previous}")
                + f" ({lengths[previous]}h, {shift_span(previous, lengths[previous])})"
                f" — it would start inside it. "
                f"Set {current} to {pattern_hours(lengths[previous])}h or more, or "
                + (f"make {previous} a short day too." if short else f"shorten {previous}.")
            )
    return None


def _slot_day_offset(slot: str) -> int:
    """1 if the rounds file this slot under the next morning, else 0."""
    return 1 if slot in NIGHT_SLOTS else 0


def shift_slot_dates(
    shift: str, production_day: date_type, shift_hours: int = DEFAULT_SHIFT_HOURS
) -> list[tuple[str, date_type]] | None:
    """The (slot, calendar date) checkpoints of one shift, or None if the
    shift doesn't fit in the day.

    Each date is the one the rounds filed the reading under, which is the
    next morning for 12AM-6AM slots.
    """
    slots = shift_slots(shift, shift_hours)
    if slots is None:
        return None
    return [
        (slot, production_day + timedelta(days=_slot_day_offset(slot)))
        for slot in slots
    ]


def _hour_label(hour: int) -> str:
    """24h hour to a slot-style label ("6PM")."""
    hour %= 24
    if hour == 0:
        return "12AM"
    if hour < 12:
        return f"{hour}AM"
    if hour == 12:
        return "12PM"
    return f"{hour - 12}PM"


def shift_span(shift: str, shift_hours: int = DEFAULT_SHIFT_HOURS) -> str | None:
    """Clock span of `shift` at that length, e.g. "6PM-6AM", or None if it
    doesn't fit. On a short day this is the scheduled part ("12PM-4PM"); see
    window_span() for the whole window."""
    if shift_slots(shift, shift_hours) is None:
        return None
    start = DAY_START_HOUR + SHIFT_ORDER.index(shift) * pattern_hours(shift_hours)
    return f"{_hour_label(start)}-{_hour_label(start + shift_hours)}"


def window_span(shift: str, shift_hours: int = DEFAULT_SHIFT_HOURS) -> str | None:
    """The whole window the shift's checkpoints come from. Differs from
    shift_span() only on a short day."""
    return shift_span(shift, pattern_hours(shift_hours))


def default_scheduled(shift: str, shift_hours: int = DEFAULT_SHIFT_HOURS) -> bool:
    """Whether a machine counts as scheduled for `shift` when nobody has said
    otherwise.

    Any 8-hour shift and any 1st Shift: yes. A 10/12-hour 2nd Shift is a
    night crew, which is the exception, so it defaults to no; the same goes
    for a short day's 2nd and 3rd Shifts. Without this, every long-day
    machine would show a missing night.
    """
    if shift_slots(shift, shift_hours) is None:
        return False
    return shift == SHIFT_ORDER[0] or shift_hours == DEFAULT_SHIFT_HOURS


def long_length_options(shift: str) -> list[int]:
    """Which of 8/10/12 hours `shift` can be. The 3rd Shift can only be 8,
    since a longer one would run past 6AM."""
    return [hours for hours in SHIFT_LENGTH_HOURS if shift_slots(shift, hours) is not None]


def default_production_day(shift: str, now: datetime) -> date_type:
    """Which production day /console/oee opens on for `shift`.

    3rd Shift is entered the morning after it started, so before noon it
    defaults to yesterday. Otherwise it's today, unless it's still before
    6AM.
    """
    today = now.date()
    if shift == SHIFT_ORDER[-1] and now.hour < 12:
        return today - timedelta(days=1)
    if now.hour < DAY_START_HOUR:
        return today - timedelta(days=1)
    return today


def elapsed_dated_slots(
    dated_slots: list[tuple[str, date_type]], now: datetime
) -> list[str]:
    """Which of a shift's (slot, date) checkpoints have finished.

    A slot has elapsed once its window closes (the 8AM slot at 8:00). Keeps
    /oee from reporting the rest of today as missing data.
    """
    return [
        slot
        for slot, slot_date in dated_slots
        if datetime.combine(slot_date, datetime.min.time()).replace(
            hour=SLOT_END_HOURS[slot]
        ) <= now
    ]


def elapsed_slots(shift: str, production_day: date_type, now: datetime) -> list[str]:
    """elapsed_dated_slots() for an ordinary 8-hour shift."""
    dated = shift_slot_dates(shift, production_day)
    return elapsed_dated_slots(dated or [], now)


class ShiftScrapCreate(BaseModel):
    """Total scrap for one machine for one whole shift. Not cumulative."""

    machine_id: str
    entry_date: date_type
    shift: str
    scrap_units: int = Field(ge=0)
    entered_by: str = Field(min_length=1, max_length=20)


class DowntimeReasonInput(BaseModel):
    """One (reason, minutes) pair inside a downtime submission.

    Minutes must be positive. The cap here is the longest possible shift;
    create_shift_downtime() applies the machine's real shift length.
    """

    reason_code: str
    minutes: int = Field(gt=0, le=MAX_SHIFT_MINUTES)


class ShiftDowntimeCreate(BaseModel):
    """One downtime submission for a machine for one whole shift.

    An empty `reasons` list means "ran clean, no downtime". That's different
    from no submission at all, which means nobody has entered the shift yet
    and OEE shows N/A rather than assuming 100% availability.

    A submission replaces the shift's whole reason set, so a reason entered
    by mistake can be removed.
    """

    machine_id: str
    entry_date: date_type
    shift: str
    reasons: list[DowntimeReasonInput] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)
    entered_by: str = Field(min_length=1, max_length=20)


class ScheduleCreate(BaseModel):
    """A scheduling exception for one machine for one whole shift.

    Only exceptions are stored; the default comes from default_scheduled().
    A machine that wasn't scheduled is left out of the OEE rollup instead of
    scoring 0%.
    """

    machine_id: str
    entry_date: date_type
    shift: str
    scheduled: bool
    entered_by: str = Field(min_length=1, max_length=20)


class ShiftLengthCreate(BaseModel):
    """How many hours one shift ran on one machine on one production day.

    `shift_hours` is 8, 10 or 12, or 1-6 for a short day. The as-long-or-
    longer rule is checked by the caller (shift_length_conflict()), since
    this model only sees one shift.
    """

    machine_id: str
    entry_date: date_type
    shift: str
    shift_hours: int
    entered_by: str = Field(min_length=1, max_length=20)

    @field_validator("shift")
    @classmethod
    def _real_shift(cls, value: str) -> str:
        if value not in SHIFT_ORDER:
            raise ValueError(f"shift must be one of {SHIFT_ORDER}")
        return value

    @field_validator("shift_hours")
    @classmethod
    def _known_length(cls, value: int) -> int:
        if value not in VALID_SHIFT_HOURS:
            raise ValueError(f"shift_hours must be one of {VALID_SHIFT_HOURS}")
        return value


# The boards use SHIFT_SLOTS and OEE uses shift_plan(); they must agree.
assert shift_plan(DEFAULT_SHIFT_HOURS) == SHIFT_SLOTS, (
    "shift_plan(8) must equal SHIFT_SLOTS - TIME_SLOTS or SHIFT_SLOTS was reordered"
)
