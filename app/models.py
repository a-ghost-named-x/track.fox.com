"""Models and fixed reference lists for the manual-entry (floor) dashboard."""
from datetime import date as date_type

from pydantic import BaseModel, Field

# Fixed time-slot list, in display order. Matches the slot values seeded
# into the `standards` table (sql/03_seed_standards.sql).
TIME_SLOTS: list[str] = [
    "8AM", "10AM", "12PM", "2PM", "4PM", "6PM",
    "8PM", "10PM", "12AM", "2AM", "4AM", "6AM",
]

# Full machine roster (23 total), matching the seeded `standards` table.
MACHINE_IDS: list[str] = [
    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11",
    "C14", "C15", "C16",
    "FM1", "FM2", "FM3",
    "WS1", "WS2", "WS3", "WS4", "WS5", "WS6",
]

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
# in app/routers/dashboard.py, which applies it).
SHIFT_DISPLAY_DELAY_HOURS: int = 1

# Which 4 of the 12 TIME_SLOTS columns to display for a given shift. The
# dashboard grid shows only the current shift's slots rather than all 12.
SHIFT_SLOTS: dict[str, list[str]] = {
    "1st Shift": ["8AM", "10AM", "12PM", "2PM"],
    "2nd Shift": ["4PM", "6PM", "8PM", "10PM"],
    "3rd Shift": ["12AM", "2AM", "4AM", "6AM"],
}


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
