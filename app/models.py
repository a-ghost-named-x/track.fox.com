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
# P1-P4 and A1-A7 are new machines with no row in the `standards` table yet
# (standards are on the way — see DASHBOARD_ZONES). They're listed here so
# they show up in /console and on their dashboard zones now, but until
# someone adds their standards rows, submitting an entry for any of them
# will fail with StandardNotFoundError (app/db/entries.py) — that's the
# existing, intentional "can't compute status" guard, not a bug.
MACHINE_IDS: list[str] = [
    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11",
    "C14", "C15", "C16",
    "FM1", "FM2", "FM3",
    "WS1", "WS2", "WS3", "WS4", "WS5", "WS6",
    "P1", "P2", "P3", "P4",
    "A1", "A2", "A3", "A4", "A5", "A6", "A7",
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
    "leno": ["A1", "A2", "A3", "A4", "A5", "A6", "A7"],
}

# Display label per zone slug, for the /dashboard index page and each zone
# page's header.
DASHBOARD_ZONE_LABELS: dict[str, str] = {
    "b2": "B2",
    "b3": "B3",
    "b4": "B4",
    "ws": "WS",
    "leno": "Leno",
}

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
