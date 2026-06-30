"""Models and fixed reference lists for the manual-entry (floor) dashboard."""
from datetime import date as date_type

from pydantic import BaseModel, Field

# Fixed time-slot list, in display order. Placeholder per architecture
# discussion — confirm this is the final list before going live, and note
# the seed data only covers 6 of these 12 slots for machine C1 so far.
TIME_SLOTS: list[str] = [
    "6AM", "8AM", "10AM", "12PM", "2PM", "4PM",
    "6PM", "8PM", "10PM", "12AM", "2AM", "4AM",
]

# Placeholder machine list for the /console dropdown. Replace with the real
# machine roster (could also be loaded from the standards table instead of
# hardcoded, once the full list is finalized).
MACHINE_IDS: list[str] = ["C1", "C2"]


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
