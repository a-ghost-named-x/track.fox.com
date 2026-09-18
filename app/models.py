"""Models and fixed reference lists for the manual-entry (floor) dashboard."""
from datetime import date as date_type
from datetime import datetime, timedelta

from pydantic import BaseModel, Field, field_validator

# Fixed time-slot list, in display order. Matches the slot values seeded
# into the `standards` table (sql/03_seed_standards.sql).
TIME_SLOTS: list[str] = [
    "8AM", "10AM", "12PM", "2PM", "4PM", "6PM",
    "8PM", "10PM", "12AM", "2AM", "4AM", "6AM",
]

# Full machine roster. Used by /console's machine dropdown and as the
# superset DASHBOARD_ZONES below is drawn from.
#
# Every machine listed here has real standards seeded: the original 23 in
# sql/03_seed_standards.sql, and P1-P4 (Poly) + AS1-AS7 (Leno) in
# sql/04_seed_new_machines_standards.sql.
#
# The Leno machines are AS1-AS7, not A1-A7 — "AS" is the floor's own naming,
# per the standards spreadsheet. An earlier revision of this list had them as
# A1-A7, which is also what the all-zero placeholder rows in 04 were seeded
# under; sql/05_drop_legacy_leno_machine_ids.sql removes those orphans.
#
# Adding a machine here without seeding its standards first makes every entry
# for it fail with StandardNotFoundError (app/db/entries.py) — the deliberate
# "can't compute status" guard. Seeding it with a standard of 0 is worse: the
# entry saves and the cell is permanently green, since compute_status() is
# `units_produced >= standard_units`. Seed real numbers, then list it.
MACHINE_IDS: list[str] = [
    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11",
    "C14", "C15", "C16",
    "FM1", "FM2", "FM3",
    "WS1", "WS2", "WS3", "WS4", "WS5", "WS6",
    "P1", "P2", "P3", "P4",
    "AS1", "AS2", "AS3", "AS4", "AS5", "AS6", "AS7",
]

# Dashboard zones — each one is a physical floor-section display, reachable
# at /dashboard/<slug> (e.g. /dashboard/b3), showing only its own machines'
# rows out of the full MACHINE_IDS roster above. /dashboard itself lists
# these as links rather than rendering a single all-machines grid.
DASHBOARD_ZONES: dict[str, list[str]] = {
    "b2": ["FM1", "FM2", "FM3"],
    "b3": ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C14", "C15", "C16"],
    "b4": ["P1", "P2", "P3", "P4"],
    "ws": ["WS1", "WS2", "WS3", "WS4", "WS5", "WS6"],
    "leno": ["AS1", "AS2", "AS3", "AS4", "AS5", "AS6", "AS7"],
}

# Display label per zone slug, for the /dashboard index page and each zone
# page's header.
DASHBOARD_ZONE_LABELS: dict[str, str] = {
    "b2": "FM",
    "b3": "Combo/FMW",
    "b4": "Poly",
    "ws": "WS",
    "leno": "Leno",
}

# Order the /supervisor page stacks its zone sections in, top to bottom.
# Deliberately its own list rather than reusing DASHBOARD_ZONES' key order:
# Combo/FMW leads because it's the largest section and the one supervisors
# read first, then FM and WS (the two zones sharing its standards set), then
# Poly and Leno, which each have their own. Reorder this list to reorder the
# page — nothing else depends on it.
#
# A slug here that's missing from DASHBOARD_ZONES is skipped rather than
# raising, so retiring a zone from DASHBOARD_ZONES can't 500 /supervisor.
SUPERVISOR_ZONE_ORDER: list[str] = ["b3", "b2", "ws", "b4", "leno"]

# Three fixed 8-hour shifts covering the full day, keyed by the server-local
# hour (24h) each shift *actually* begins. Display-only — has no bearing on
# the time_slot/standards logic above.
SHIFTS: list[tuple[int, str]] = [
    (6, "1st Shift"),   # 6AM - 2PM
    (14, "2nd Shift"),  # 2PM - 10PM
    (22, "3rd Shift"),  # 10PM - 6AM
]

# How many hours after a shift's real start the dashboard should keep
# showing the *outgoing* shift, giving the incoming crew a window to review
# the previous shift's production before the display switches over. E.g.
# with a value of 1, 2nd Shift actually starts at 2PM, but the dashboard
# doesn't switch to it until 3PM. Change this single number to adjust the
# buffer for all three shift changeovers at once (see get_current_shift()
# below, which applies it).
SHIFT_DISPLAY_DELAY_HOURS: int = 1

# Which 4 of the 12 TIME_SLOTS columns to display for a given shift. The
# dashboard grid shows only the current shift's slots rather than all 12.
# Also used by /console/<zone>'s batch entry form to narrow its time-slot
# picker to just the 4 slots that make sense to log right now.
SHIFT_SLOTS: dict[str, list[str]] = {
    "1st Shift": ["8AM", "10AM", "12PM", "2PM"],
    "2nd Shift": ["4PM", "6PM", "8PM", "10PM"],
    "3rd Shift": ["12AM", "2AM", "4AM", "6AM"],
}

# Shift labels in the order /console/<zone>'s shift toggle lays them out.
# Derived from SHIFTS rather than written out again so the two can't drift.
SHIFT_ORDER: list[str] = [label for _, label in SHIFTS]

# The extra fourth option on /supervisor's shift toggle: show all 12 slot
# columns at once instead of one shift's 4. Deliberately kept OUT of
# SHIFT_ORDER and SHIFT_SLOTS — it isn't a real shift, and those two are read
# by /console and /dashboard for genuine shift logic, where an "All Day"
# value would be meaningless at best and saved onto an entry at worst.
ALL_DAY_LABEL: str = "All Day"


def get_current_shift(now: datetime) -> str:
    """Returns the label of whichever shift is in progress / should be
    displayed at `now`, using server-local time. Shared by the dashboard
    routes (which shift's grid to show) and /console/<zone>'s batch entry
    form (which 4 time slots make sense to offer right now).

    Each shift's real start hour is pushed back by SHIFT_DISPLAY_DELAY_HOURS
    before comparing, so the dashboard keeps showing the outgoing shift for
    that many hours past its real changeover — giving the incoming crew time
    to review the outgoing shift's production before the display switches.

    SHIFTS is sorted by start hour; we walk it and keep the last (delayed)
    boundary that `now` has passed. Hours before the first boundary fall
    through to the final shift in the list, since that shift wraps past
    midnight (10PM-6AM).
    """
    current = SHIFTS[-1][1]
    for start_hour, label in SHIFTS:
        display_hour = (start_hour + SHIFT_DISPLAY_DELAY_HOURS) % 24
        if now.hour >= display_hour:
            current = label
    return current


def resolve_shift(requested: str | None, now: datetime) -> str:
    """Which shift a /console/<zone> page should be logging against.

    `requested` is whatever the user picked with that page's shift toggle —
    a query param on GET, a hidden field on POST. Anything unrecognized
    (None on a first visit, a hand-edited URL, a renamed shift) falls back
    to get_current_shift(), so the page still opens on the sensible default
    for the time of day.

    Deliberately kept separate from get_current_shift(): the dashboard must
    always follow the clock, but /console can't, because the two have
    opposite needs at a changeover. SHIFT_DISPLAY_DELAY_HOURS rolls the
    dashboard over to 2nd Shift at 3PM, and that same rollover used to take
    1st Shift's time slots out of the console's dropdown with it — locking
    people out of entering 1st Shift numbers they hadn't finished collecting
    until 4PM. The console picks its shift from this function instead, so a
    display rule can't decide what's still enterable.
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
    """A stored entry, including server-computed/assigned fields."""

    id: int
    status: str  # ":)" or ":("


# ---------------------------------------------------------------------------
# OEE — reference values and slot geometry
#
# OEE = Availability x Performance x Quality, computed in app/db/oee.py.
# Schema and the full derivation are in docs/sql/06_oee_schema.sql and
# docs/sql/08_seed_ideal_rates.sql.
# ---------------------------------------------------------------------------

# Every time slot is the END of a 2-hour window (the 8AM slot covers 6-8AM),
# so four slots tile one 8-hour shift exactly: 4 x 120 = 480 minutes.
#
# This is also Planned Production Time for a fully-scheduled slot. Per the
# floor, machines do NOT stop for breaks or lunch — someone covers the machine
# so it keeps running — so there is no break allowance to deduct here. If that
# ever changes, it becomes planned downtime via the reason codes, not a change
# to this constant.
SLOT_MINUTES: int = 120

# One whole shift, which is the grain OEE is now captured and reported at.
# Derived from the slot geometry rather than written as 480, so the two can't
# drift if a shift ever gains or loses a checkpoint.
#
# Every shift has the same number of slots, so any of them will do for the
# count; SHIFT_SLOTS is validated as four-per-shift by the standards seed.
SHIFT_MINUTES: int = SLOT_MINUTES * len(SHIFT_SLOTS[SHIFT_ORDER[0]])

# Standards are set at 75% of each machine's theoretical maximum. Recorded here
# for readers; nothing computes with it. The actual ceiling used by the OEE
# math lives per-machine in the `machine_ideal_rates` table, seeded by
# docs/sql/08_seed_ideal_rates.sql, precisely so that a target change can't
# silently move historical OEE. See that file for the arithmetic.
STANDARD_PCT_OF_IDEAL: float = 0.75


def _slot_end_hour(slot: str) -> int:
    """Converts a slot label to the 24h hour its window closes on.

    "8AM" -> 8, "2PM" -> 14, "12AM" -> 0, "12PM" -> 12. Parsed rather than
    written out as a literal map so it cannot drift from TIME_SLOTS.
    """
    meridiem = slot[-2:]
    hour = int(slot[:-2])
    if meridiem == "AM":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


SLOT_END_HOURS: dict[str, int] = {slot: _slot_end_hour(slot) for slot in TIME_SLOTS}

# Which shift a slot belongs to, and its 1-based position within that shift.
# Position is what makes cumulative deltas work: units_produced resets at the
# start of every shift, so slot 1's delta is its own value and slot n's is the
# difference from slot n-1.
#
# Derived from SHIFT_SLOTS rather than written out again, so reordering a
# shift's slots there can't leave a stale copy here.
SLOT_POSITIONS: dict[str, tuple[str, int]] = {
    slot: (label, index)
    for label, slots in SHIFT_SLOTS.items()
    for index, slot in enumerate(slots, start=1)
}

# Order the /oee page stacks its zone sections in. Same order /supervisor
# uses — /oee is the same audience at the same desk, and having the two pages
# disagree about where Poly sits would be its own small papercut.
OEE_ZONE_ORDER: list[str] = SUPERVISOR_ZONE_ORDER


# ---------------------------------------------------------------------------
# Shift LENGTH — for OEE only.
#
# With the current staffing a machine is often run for 10 or 12 hours by one
# crew instead of three 8-hour shifts (production manager, 2026-09-16, refined
# 2026-09-18). The 2-hour rounds and the floor screens stay on the 8-hour
# rotation regardless: the crew keeps writing the running count into the 4PM
# and 6PM boxes, and the 2nd Shift board compares those against a standard
# that assumes a fresh counter, so its colours are meaningless on a machine
# running long. The floor knows that and lives with it. OEE, though, has to
# judge the machine against the minutes it actually ran, which is what this
# section provides.
#
# THE RULES, all from the floor:
#
#   - The work day ALWAYS starts at 6AM.
#   - A shift's LENGTH decides WHERE IT STARTS: the Nth L-hour shift of the
#     day starts at 6AM + N x L. So the second 12-hour shift is 6PM-6AM, the
#     second 10-hour shift is 4PM-2AM, the second 8-hour shift is 2PM-10PM.
#     A 12-hour crew never starts at 2PM. (Stated as fact, 2026-09-18.)
#   - Each shift is set on its own, per machine per day. C1 can run an
#     8-hour 1st Shift, sit idle 2PM-6PM, and have a 12-hour crew come in at
#     6PM — that is 1st = 8h, 2nd = 12h. Tomorrow it can be 8/8/8 again.
#   - A later shift can be AS LONG OR LONGER than the one before it, never
#     shorter. A 12-hour 1st Shift runs to 6PM; an 8-hour 2nd Shift would
#     start at 2PM inside it and count 4PM and 6PM twice. That single rule
#     makes every combination either valid or an overlap: 8/8/8, 8/10, 8/12,
#     10/10, 10/12, 12/12 — and a 3rd Shift only ever exists on an all-8h day.
#   - A shift with no length set inherits the one before it (1st defaults to
#     8), so "decide at 6AM that today is 12 hours" is one click and the
#     night falls out of it.
#
# Stored per (machine, production day, shift) in `machine_shift_length`
# (docs/sql/14_shift_length.sql, then 15); absence means inherit. Set on
# /console/oee.
#
# THE PRODUCTION DAY
# ------------------
# Every review-side date (/oee, /supervisor, /console/oee) is the day the
# shift STARTED: "3rd Shift, Thursday" is Thursday 10PM through Friday 6AM.
# The 2-hour rounds and the floor screens still date the 12AM-6AM
# checkpoints by the morning they land on — that is how `entries` and the
# boards' "today" filter have always worked, and re-keying an append-only
# history table is not worth the risk. shift_slot_dates() is the bridge: it
# hands back each checkpoint with the calendar date the rounds filed it
# under, and the review side reads two dates and stitches the shift back
# together.
# ---------------------------------------------------------------------------

SHIFT_LENGTH_HOURS: list[int] = [8, 10, 12]
DEFAULT_SHIFT_HOURS: int = 8

# The outer bound on a downtime reason's minutes, matching the CHECK on
# shift_downtime_reason. The real cap is the machine's own shift length on
# that day, enforced at write time by create_shift_downtime().
MAX_SHIFT_MINUTES: int = max(SHIFT_LENGTH_HOURS) * 60

# The hour the production day starts, taken from the 1st Shift's real start
# rather than written as 6 again. Everything about long shifts hangs off it.
DAY_START_HOUR: int = SHIFTS[0][0]

# Slots whose window closes at or before the day-start hour (12AM, 2AM, 4AM,
# 6AM): they belong to the production day that started the evening before,
# and the rounds file them under the NEXT calendar date.
NIGHT_SLOTS: list[str] = [
    slot for slot in TIME_SLOTS if SLOT_END_HOURS[slot] <= DAY_START_HOUR
]


def shift_slots(shift: str, shift_hours: int) -> list[str] | None:
    """The checkpoints of `shift` when that shift is `shift_hours` long, or
    None if the day has no room for it.

    The Nth L-hour shift starts at 6AM + N x L, so it is simply the Nth chunk
    of L/2 slots out of TIME_SLOTS (which starts at 8AM because the day
    starts at 6AM):

        1st, 8h  -> 8AM..2PM      2nd, 8h  -> 4PM..10PM    3rd, 8h -> 12AM..6AM
        1st, 10h -> 8AM..4PM      2nd, 10h -> 6PM..2AM     3rd, 10h: none
        1st, 12h -> 8AM..6PM      2nd, 12h -> 8PM..6AM     3rd, 12h: none

    A chunk that runs off the end of the day (a 10-hour 3rd Shift would need
    4AM through 10AM) is not a shift; the leftover hours are idle time.
    """
    per_shift = shift_hours * 60 // SLOT_MINUTES
    index = SHIFT_ORDER.index(shift)
    chunk = TIME_SLOTS[index * per_shift:(index + 1) * per_shift]
    return chunk if len(chunk) == per_shift else None


def shift_plan(shift_hours: int) -> dict[str, list[str]]:
    """Every shift's slots on a day where ALL shifts are `shift_hours` long.
    The 8-hour plan reproduces SHIFT_SLOTS exactly — asserted at the bottom
    of this file. Mostly a convenience for tests and readers; the report
    resolves each shift's length separately with resolve_day_lengths()."""
    plan: dict[str, list[str]] = {}
    for label in SHIFT_ORDER:
        slots = shift_slots(label, shift_hours)
        if slots is not None:
            plan[label] = slots
    return plan


def resolve_day_lengths(explicit: dict[str, int]) -> dict[str, int]:
    """Each shift's length for one machine on one day, given whatever was
    actually set. The 1st Shift defaults to 8; every later shift defaults to
    the one before it, so setting 1st to 12 makes the night a 12-hour 2nd
    Shift without a second click, and 8/8/8 needs no rows at all.

    Returns a length for all three shifts even when a shift can't exist at
    that length (a 12-hour 3rd Shift); shift_slots() is the existence test.
    """
    lengths: dict[str, int] = {}
    previous = DEFAULT_SHIFT_HOURS
    for label in SHIFT_ORDER:
        previous = explicit.get(label, previous)
        lengths[label] = previous
    return lengths


def shift_length_conflict(lengths: dict[str, int]) -> str | None:
    """The as-long-or-longer rule, as a message naming the overlap, or None.

    A later shift shorter than the one before it starts inside it: a 12-hour
    1st Shift runs to 6PM, an 8-hour 2nd Shift starts at 2PM. Checked on
    every save of /console/oee and re-checked here rather than only in the
    form, so no path can store an overlapping day.
    """
    for previous, current in zip(SHIFT_ORDER, SHIFT_ORDER[1:]):
        if lengths[current] < lengths[previous]:
            return (
                f"{current} ({lengths[current]}h, {shift_span(current, lengths[current])}) "
                f"can't be shorter than {previous} ({lengths[previous]}h, "
                f"{shift_span(previous, lengths[previous])}) — it would start inside it. "
                f"Set {current} to {lengths[previous]}h or more, or shorten {previous}."
            )
    return None


def _slot_day_offset(slot: str) -> int:
    """0 if the rounds file this slot under the production day itself, 1 if
    under the morning after (NIGHT_SLOTS)."""
    return 1 if slot in NIGHT_SLOTS else 0


def shift_slot_dates(
    shift: str, production_day: date_type, shift_hours: int = DEFAULT_SHIFT_HOURS
) -> list[tuple[str, date_type]] | None:
    """The (slot, calendar date) checkpoints of one shift on one production
    day, or None if a shift of that length has no room in the day.

    The date on each pair is the one the 2-hour rounds filed it under, which
    for the 12AM-6AM slots is the morning after. Every 3rd Shift straddles
    midnight this way, and so does a 10 or 12-hour 2nd Shift. The review side
    reads both dates and stitches the shift back together from this list.
    """
    slots = shift_slots(shift, shift_hours)
    if slots is None:
        return None
    return [
        (slot, production_day + timedelta(days=_slot_day_offset(slot)))
        for slot in slots
    ]


def _hour_label(hour: int) -> str:
    """24h hour -> the slot-style label the rest of the app uses ("6PM")."""
    hour %= 24
    if hour == 0:
        return "12AM"
    if hour < 12:
        return f"{hour}AM"
    if hour == 12:
        return "12PM"
    return f"{hour - 12}PM"


def shift_span(shift: str, shift_hours: int = DEFAULT_SHIFT_HOURS) -> str | None:
    """Clock span of `shift` at that length, e.g. "6PM-6AM", or None if the
    day has no room for it. Display only."""
    if shift_slots(shift, shift_hours) is None:
        return None
    start = DAY_START_HOUR + SHIFT_ORDER.index(shift) * shift_hours
    return f"{_hour_label(start)}-{_hour_label(start + shift_hours)}"


def default_scheduled(shift: str, shift_hours: int = DEFAULT_SHIFT_HOURS) -> bool:
    """Whether a machine counts as scheduled for `shift` when nobody has said
    otherwise on /console/oee.

    On an 8-hour shift the answer is yes, which is the machine_schedule rule
    of "absence means scheduled". A 10 or 12-hour 2nd Shift is a night crew,
    and a night crew is the exception rather than the rule — more often the
    machine is off until 6AM — so it defaults to NOT scheduled and ticking
    the box (or picking the length on the 2nd Shift page, which ticks it) is
    how one gets counted. Otherwise every long-day machine would show a
    missing night every day and someone would be unticking 34 boxes.
    """
    if shift_slots(shift, shift_hours) is None:
        return False
    return shift == SHIFT_ORDER[0] or shift_hours == DEFAULT_SHIFT_HOURS


def default_production_day(shift: str, now: datetime) -> date_type:
    """Which production day /console/oee should open on for `shift`.

    The person entering 3rd Shift does it at the END of the shift — 6AM, the
    morning after it started — and the date they need is yesterday's. So a
    3rd Shift page opened any time before noon defaults to yesterday. For
    the other shifts, and for 3rd Shift opened in the evening (tonight's,
    still running), the production day is today, unless it is still before
    6AM, when the running day is yesterday's.
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
    """Which of a shift's (slot, date) checkpoints have actually finished.

    Used so /oee doesn't report the rest of today as "missing data". A slot
    has elapsed once its 2-hour window has closed — the 8AM slot at 8:00 —
    which one comparison against the window's closing datetime covers for
    past dates (all elapsed), today (only the closed ones) and future dates
    (none) alike. Carrying the date on each slot is what makes the
    post-midnight checkpoints of any overnight shift come out right.
    """
    return [
        slot
        for slot, slot_date in dated_slots
        if datetime.combine(slot_date, datetime.min.time()).replace(
            hour=SLOT_END_HOURS[slot]
        ) <= now
    ]


def elapsed_slots(shift: str, production_day: date_type, now: datetime) -> list[str]:
    """Which of `shift`'s 8-hour slots have finished on `production_day`.
    The plain form of elapsed_dated_slots() for an ordinary day."""
    dated = shift_slot_dates(shift, production_day)
    return elapsed_dated_slots(dated or [], now)




class ShiftScrapCreate(BaseModel):
    """Total scrap for one machine for one whole shift.

    NOT cumulative. At the old 2-hour grain scrap was a running total so each
    checkpoint superseded the last; there is only one reading per shift now, so
    it is simply that shift's total.

    The carry-forward rule (a blank means unchanged) still applies to
    production units on the 2-hour form, and only there — see
    _cumulative_deltas() in app/db/oee.py.
    """

    machine_id: str
    entry_date: date_type
    shift: str
    scrap_units: int = Field(ge=0)
    entered_by: str = Field(min_length=1, max_length=20)


class DowntimeReasonInput(BaseModel):
    """One (reason, minutes) pair inside a downtime submission.

    Minutes must be positive: a zero-minute reason says nothing. "Ran clean" is
    a submission with an empty `reasons` list, which is a materially different
    statement from no submission at all.

    Capped at the longest shift there is, matching the CHECK on
    shift_downtime_reason. The tighter cap — this machine's shift length on
    this day — is applied by create_shift_downtime(), which knows it.
    """

    reason_code: str
    minutes: int = Field(gt=0, le=MAX_SHIFT_MINUTES)


class ShiftDowntimeCreate(BaseModel):
    """One downtime submission for a machine for one whole shift.

    An empty `reasons` list is valid and load-bearing: it records "ran clean,
    no downtime, 100% availability". Absence of any submission means nobody has
    entered this shift yet, and OEE reports N/A rather than assuming zero. Same
    NULL-is-not-zero rule as a standards row of 0.

    A submission REPLACES the shift's whole reason set, which is what makes it
    possible to remove a reason entered by mistake.
    """

    machine_id: str
    entry_date: date_type
    shift: str
    reasons: list[DowntimeReasonInput] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=500)
    entered_by: str = Field(min_length=1, max_length=20)


class ScheduleCreate(BaseModel):
    """A scheduling exception for one machine for one whole shift.

    Only exceptions get written — absence of a row means the machine WAS
    scheduled, so nobody has to fill anything in on a normal day. A machine
    marked not-scheduled is excluded from that shift's OEE rollup entirely,
    rather than scoring 0%.
    """

    machine_id: str
    entry_date: date_type
    shift: str
    scheduled: bool
    entered_by: str = Field(min_length=1, max_length=20)


class ShiftLengthCreate(BaseModel):
    """How many hours one shift ran on one machine on one production day.

    `entry_date` is the production day — the day the 1st Shift started. Only
    non-inherited lengths need writing (absence means "same as the shift
    before", 8 for the 1st), but any value is accepted so a wrong 12 can be
    undone. The as-long-or-longer rule is checked by the caller against the
    other shifts' lengths (shift_length_conflict), not here — this model
    only sees one shift.
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
        if value not in SHIFT_LENGTH_HOURS:
            raise ValueError(f"shift_hours must be one of {SHIFT_LENGTH_HOURS}")
        return value


# The 8-hour plan must reproduce SHIFT_SLOTS, or the two halves of the app
# (the boards on SHIFT_SLOTS, OEE on shift_plan) disagree about which slots a
# shift has. Checked at import so a reordering of TIME_SLOTS can't ship.
assert shift_plan(DEFAULT_SHIFT_HOURS) == SHIFT_SLOTS, (
    "shift_plan(8) must equal SHIFT_SLOTS - TIME_SLOTS or SHIFT_SLOTS was reordered"
)
