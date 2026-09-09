"""Models and fixed reference lists for the manual-entry (floor) dashboard."""
from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, Field

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


def elapsed_slots(shift: str, entry_date: date_type, now: datetime) -> list[str]:
    """Which of `shift`'s slots have actually finished, for `entry_date`.

    Used so /oee doesn't report the rest of today as "missing data". A past
    date has all four slots elapsed; today's has only the ones whose 2-hour
    window has closed. A future date has none.

    3rd Shift needs no special handling despite spanning midnight: its slots
    (12AM-6AM) are logged against the calendar day they LAND on, which is how
    entry_date already works everywhere in the app, so their end hours (0, 2,
    4, 6) are genuinely early-morning hours of entry_date.
    """
    slots = SHIFT_SLOTS.get(shift, [])
    today = now.date()
    if entry_date < today:
        return list(slots)
    if entry_date > today:
        return []
    return [slot for slot in slots if now.hour >= SLOT_END_HOURS[slot]]


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

    Capped at one whole shift, matching the CHECK on shift_downtime_reason.
    """

    reason_code: str
    minutes: int = Field(gt=0, le=SHIFT_MINUTES)


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
