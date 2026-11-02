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
    "PB1", "PB2", "PP1",
]

# One floor screen per zone, served at /dashboard/<slug>.
DASHBOARD_ZONES: dict[str, list[str]] = {
    "b2": ["FM1", "FM2", "FM3"],
    "b3": ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11", "C14", "C15", "C16"],
    "b4": ["P1", "P2", "P3", "P4"],
    "ws": ["WS1", "WS2", "WS3", "WS4", "WS5", "WS6"],
    "leno": ["AS1", "AS2", "AS3", "AS4", "AS5", "AS6", "AS7"],
    "pouch": ["PB1", "PB2", "PP1"],
}

DASHBOARD_ZONE_LABELS: dict[str, str] = {
    "b2": "FM",
    "b3": "Combo/FMW",
    "b4": "Poly",
    "ws": "WS",
    "leno": "Leno",
    "pouch": "Pouch",
}

# Top-to-bottom zone order on /supervisor. Slugs missing from DASHBOARD_ZONES
# are skipped, so retiring a zone can't break the page.
SUPERVISOR_ZONE_ORDER: list[str] = ["b3", "b2", "ws", "b4", "leno", "pouch"]

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

# Longer periods /oee can show besides one shift: rolling windows ending on
# (and including) the selected date, each compared with the window before it.
OEE_PERIOD_DAYS: list[int] = [7, 30]

# /oee's custom period runs from a picked first day to the selected date, and
# is compared with the same number of days before it, like the presets.
# Capped because the comparison reads twice as many days.
OEE_CUSTOM_MAX_DAYS: int = 60

# Periods up to this long get a column per day; longer ones a column per week.
OEE_DAILY_COLUMNS_MAX_DAYS: int = 15


def oee_column_days(days: int) -> int:
    """How many days each of a period's columns covers. Weeks end on the
    period's last day, so when they don't divide it the oldest column holds
    the leftover days (30 days is four weeks, then two days)."""
    return 1 if days <= OEE_DAILY_COLUMNS_MAX_DAYS else 7


# /oee's option for all three shifts added together over a period.
# Like ALL_DAY_LABEL, it isn't a shift and is never stored.
ALL_SHIFTS_LABEL: str = "All"


# ---------------------------------------------------------------------------
# Shift length and start (OEE only)
#
# A machine can be run by one crew for 10 or 12 hours instead of the usual
# 8, or for only part of a shift. The 2-hour rounds and the floor screens stay
# on the 8-hour rotation (the crew keeps writing its running count into the
# boxes it's there for), but OEE has to judge the machine against the hours
# it was scheduled for, from the checkpoints that crew wrote.
#
# Each shift on a machine's day has a length and a start. With nothing set:
#   - The production day starts at 6AM, and so does the 1st Shift.
#   - A later shift starts when the one before it ends: 2PM after an 8h 1st
#     Shift, 4PM or 6PM after a 10h or 12h one, 10PM for a 3rd after an 8h
#     2nd Shift that started at 2PM.
#   - A shift's length follows the one before it (the 1st defaults to 8), but
#     never past 6AM. After a 2PM-12AM 2nd Shift the 3rd is 12AM-6AM, 6h.
#   - A 3rd Shift only exists if the 2nd ends before 6AM.
#
# The 2nd and 3rd Shift's start can be set to any later checkpoint, for a crew
# that came on after the one before it left (a 10h crew from 4PM after an 8h
# day). A shift can't start before the one before it is over, and every shift
# is over by 6AM (day_conflict()).
#
# Short shifts
# ------------
# A crew can be scheduled for only part of a shift (mostly on Saturdays): 2,
# 4 or 6 hours, since production is only read every 2 hours and an odd number
# would end between two checkpoints. A short shift keeps the checkpoints of
# the shift it would otherwise have been, its window: a 6h 2nd Shift from 2PM
# owns 4PM to 10PM, so it doesn't matter which of those boxes holds its final
# count, and the 3rd still starts at 10PM. A crew set to start inside that
# window cuts it short.
#
# The 1st Shift's short crew can start at 6, 8 or 10AM but is always done by
# 2PM. The start isn't recorded because it doesn't change the numbers: boxes
# before the crew arrives are blank (the counter was at zero), boxes after it
# leaves are blank (unchanged), so production is still the last reading.
# Pages say "within 6AM-2PM" instead of a clock time.
#
# The hours are scheduled time, not time run: Planned Production Time is the
# hours x 60, and the standard scales with them. A 6-hour shift where the
# operator missed two hours is 6 with 120 minutes of Lack of Operator;
# entering 4 would hide that loss.
#
# Each crew starts counting from zero. A later shift starts unticked unless
# it and every shift before it are 8 hours (see default_scheduled()).
#
# Lengths are stored in `machine_shift_length` and starts in
# `machine_shift_start`, per (machine, production day, shift); no row means
# the default above.
#
# Inside this module a time of day is counted in hours after 6AM (2PM is 8,
# 6AM the next morning is 24), so a day's hours run 0 to 24 without wrapping.
# Stored starts are clock hours (14 = 2PM); start_offset() converts.
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

SHORT_SHIFT_HOURS: list[int] = list(range(2, DEFAULT_SHIFT_HOURS, 2))

# Every value the app writes to machine_shift_length. Its CHECK constraint
# must accept all of these.
VALID_SHIFT_HOURS: list[int] = SHORT_SHIFT_HOURS + SHIFT_LENGTH_HOURS

# Upper bound on one downtime reason's minutes, matching the CHECK on
# shift_downtime_reason. create_shift_downtime() applies the tighter cap of
# the machine's actual shift length.
MAX_SHIFT_MINUTES: int = max(SHIFT_LENGTH_HOURS) * 60

DAY_START_HOUR: int = SHIFTS[0][0]

# Hours between two checkpoints, and in a whole production day.
SLOT_HOURS: int = SLOT_MINUTES // 60
DAY_HOURS: int = SLOT_HOURS * len(TIME_SLOTS)

# Slots closing at or before the day start (12AM, 2AM, 4AM, 6AM). They
# belong to the production day that started the evening before, but the
# rounds file them under the next calendar date.
NIGHT_SLOTS: list[str] = [
    slot for slot in TIME_SLOTS if SLOT_END_HOURS[slot] <= DAY_START_HOUR
]


def start_offset(clock_hour: int) -> int:
    """A clock hour (14 for 2PM) as hours after the day's 6AM start (8)."""
    return (clock_hour - DAY_START_HOUR) % DAY_HOURS


def clock_hour(offset: int) -> int:
    """Hours after 6AM back to a clock hour: 8 -> 14, 20 -> 2."""
    return (DAY_START_HOUR + offset) % DAY_HOURS


def day_plan(lengths: dict[str, int], starts: dict[str, int] | None = None) -> dict[str, dict]:
    """Where each shift sits on one machine's production day.

    `lengths` ({shift: hours}) and `starts` ({shift: clock hour}) hold only
    what was set; everything else is the default (see the notes above). Each
    shift gets:

        hours       scheduled hours
        window      the 8, 10 or 12-hour shift a short one sits in
        start, end  hours after 6AM; its checkpoints are the slots closing
                    after `start`, up to and including `end`
        slots       those checkpoints, or None if the day has no room for it
        hours_set, start_set   whether each was set rather than defaulted

    A day that breaks the rules (day_conflict()) still gets a plan, so the
    form can show it.
    """
    starts = starts or {}
    plan: dict[str, dict] = {}
    window = DEFAULT_SHIFT_HOURS
    previous_end = 0
    for label in SHIFT_ORDER:
        if label == SHIFT_ORDER[0]:
            start = 0
        elif label in starts:
            start = start_offset(starts[label])
        else:
            start = previous_end
        room = DAY_HOURS - start
        hours = lengths.get(label)
        if hours is None:
            hours = min(window, room) if room >= SLOT_HOURS else window
        if hours >= DEFAULT_SHIFT_HOURS:
            window = hours
        previous_end = min(start + window, DAY_HOURS)
        plan[label] = {
            "hours": hours,
            "window": window,
            "start": start,
            "end": previous_end,
            "hours_set": label in lengths,
            "start_set": label in starts,
        }

    # A shift's checkpoints stop where the next one starts. That only moves
    # anything when a crew was set to start inside a short shift's window.
    for current, following in zip(SHIFT_ORDER, SHIFT_ORDER[1:]):
        shift = plan[current]
        shift["end"] = max(shift["start"], min(shift["end"], plan[following]["start"]))

    for shift in plan.values():
        fits = shift["start"] + SLOT_HOURS <= DAY_HOURS
        shift["slots"] = (
            TIME_SLOTS[shift["start"] // SLOT_HOURS:shift["end"] // SLOT_HOURS] if fits else None
        )
    return plan


def _busy_until(shift: str, plan: dict[str, dict]) -> int:
    """When `shift`'s crew is done, in hours after 6AM. A short 1st Shift's
    crew can be there until 2PM, since its start isn't recorded."""
    entry = plan[shift]
    if shift == SHIFT_ORDER[0]:
        return min(entry["window"], DAY_HOURS)
    return entry["start"] + entry["hours"]


def offset_label(offset: int) -> str:
    """Hours after 6AM to a slot-style label: 8 -> "2PM"."""
    return _hour_label(DAY_START_HOUR + offset)


def _length_text(shift: str, plan: dict[str, dict]) -> str:
    """A shift's length for messages: "12h, 6AM-6PM", or "4h, 6PM-10PM,
    inside 6PM-6AM" for a short shift whose checkpoints run past its hours."""
    text = f"{plan[shift]['hours']}h, {shift_span(shift, plan)}"
    whole = window_span(shift, plan)
    if shift != SHIFT_ORDER[0] and whole != shift_span(shift, plan):
        text += f", inside {whole}"
    return text


def day_conflicts(plan: dict[str, dict]) -> list[str]:
    """Every way the day doesn't fit, as messages for the form.

    A later shift can't start before the one before it is over, and every
    shift has to be over by 6AM. A shift with nothing set that has no room
    simply doesn't exist that day, so it isn't a conflict.
    """
    messages = []
    for previous, current in zip(SHIFT_ORDER, SHIFT_ORDER[1:]):
        shift = plan[current]
        busy = _busy_until(previous, plan)
        if shift["slots"] is not None and shift["start"] < busy:
            messages.append(
                f"{current} can't start at {offset_label(shift['start'])}: {previous} "
                f"({_length_text(previous, plan)}) runs until {offset_label(busy)}. "
                f"Start {current} at {offset_label(busy)} or later, or shorten {previous}."
            )

    for index, label in enumerate(SHIFT_ORDER):
        shift = plan[label]
        if shift["start"] + shift["hours"] <= DAY_HOURS:
            continue
        if shift["slots"] is not None:
            messages.append(
                f"{label} ({shift['hours']}h from {offset_label(shift['start'])}) "
                f"would run past 6AM. Make it {DAY_HOURS - shift['start']}h or less, "
                "or start it earlier."
            )
        elif shift["hours_set"]:
            previous = SHIFT_ORDER[index - 1]
            # The shift to shorten is the one that set the window: `previous`,
            # or the long shift before it when `previous` is short.
            source = next(
                (earlier for earlier in reversed(SHIFT_ORDER[:index])
                 if plan[earlier]["hours"] == plan[previous]["window"]),
                previous,
            )
            messages.append(
                f"{label} ({shift['hours']}h) has no room: {previous} "
                f"({_length_text(previous, plan)}) covers the night until 6AM. "
                f"Set when {label} started on its page, or shorten {source}."
            )
    return messages


def day_conflict(plan: dict[str, dict]) -> str | None:
    """The first of day_conflicts(), or None when the day fits."""
    messages = day_conflicts(plan)
    return messages[0] if messages else None


def new_conflict(before: dict[str, dict], after: dict[str, dict]) -> str | None:
    """The first conflict `after` has that `before` didn't, or None.

    A day can already break the rules, for instance a short 3rd Shift set
    before the 2nd was lengthened. Holding that against every other change
    would lock the whole day, so only a change that makes things worse is
    refused.
    """
    existing = set(day_conflicts(before))
    return next((m for m in day_conflicts(after) if m not in existing), None)


def start_options(shift: str, plan: dict[str, dict]) -> list[int]:
    """The clock hours `shift` can start at: every checkpoint from when the
    shift before it is over until 4AM. Empty for the 1st Shift, and for a
    3rd Shift after a 2nd that runs until 6AM."""
    index = SHIFT_ORDER.index(shift)
    if index == 0:
        return []
    earliest = _busy_until(SHIFT_ORDER[index - 1], plan)
    return [clock_hour(offset) for offset in range(earliest, DAY_HOURS, SLOT_HOURS)]


def default_start(shift: str, lengths: dict[str, int], starts: dict[str, int]) -> int:
    """The clock hour `shift` starts at when its own start isn't set, given
    the rest of the machine's day."""
    others = {label: hour for label, hour in starts.items() if label != shift}
    return clock_hour(day_plan(lengths, others)[shift]["start"])


def slot_dates(slots: list[str], production_day: date_type) -> list[tuple[str, date_type]]:
    """Each checkpoint with the calendar date the rounds filed it under,
    which is the next morning for 12AM-6AM."""
    return [
        (slot, production_day + timedelta(days=1 if slot in NIGHT_SLOTS else 0))
        for slot in slots
    ]


def shift_slot_dates(
    shift: str, production_day: date_type, plan: dict[str, dict] | None = None
) -> list[tuple[str, date_type]] | None:
    """The (slot, calendar date) checkpoints of one shift (an ordinary 8-hour
    day unless a day_plan() is given), or None if the shift doesn't fit."""
    slots = (plan or day_plan({}))[shift]["slots"]
    return None if slots is None else slot_dates(slots, production_day)


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


def shift_span(shift: str, plan: dict[str, dict] | None = None) -> str | None:
    """Clock span of `shift`'s scheduled hours, e.g. "2PM-12AM", or None if
    it doesn't fit (an ordinary 8-hour day unless a day_plan() is given).

    A short 1st Shift reads "within 6AM-2PM", since its crew can start at 6,
    8 or 10AM. See window_span() for a short shift's whole window."""
    entry = (plan or day_plan({}))[shift]
    if entry["slots"] is None:
        return None
    if shift == SHIFT_ORDER[0] and entry["hours"] < DEFAULT_SHIFT_HOURS:
        return f"within {offset_label(0)}-{offset_label(entry['window'])}"
    return f"{offset_label(entry['start'])}-{offset_label(entry['start'] + entry['hours'])}"


def window_span(shift: str, plan: dict[str, dict] | None = None) -> str | None:
    """Clock span of the checkpoints `shift` owns. Differs from shift_span()
    only on a short shift."""
    entry = (plan or day_plan({}))[shift]
    if entry["slots"] is None:
        return None
    return f"{offset_label(entry['start'])}-{offset_label(entry['end'])}"


def default_scheduled(shift: str, plan: dict[str, dict]) -> bool:
    """Whether a machine counts as scheduled for `shift` when nobody has said
    otherwise, given its day_plan().

    The 1st Shift: yes. A later shift: only if it and every shift before it
    are 8 hours. A crew after a long shift is the exception, and a day with a
    short shift is usually a Saturday where only some crews run. Without
    this, those machines would show a missing shift.
    """
    if plan[shift]["slots"] is None:
        return False
    index = SHIFT_ORDER.index(shift)
    return index == 0 or all(
        plan[label]["hours"] == DEFAULT_SHIFT_HOURS for label in SHIFT_ORDER[:index + 1]
    )


def long_length_options(shift: str) -> list[int]:
    """Which of 8/10/12 hours `shift`'s page offers. The 3rd Shift only
    offers 8: a night crew of 10 or 12 hours is entered as the 2nd Shift."""
    if shift == SHIFT_ORDER[-1]:
        return [DEFAULT_SHIFT_HOURS]
    return SHIFT_LENGTH_HOURS


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

    `shift_hours` is 8, 10 or 12, or 2, 4 or 6 for a short shift. Whether
    the day still fits is checked by the caller (day_conflict()), since this
    model only sees one shift.
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


class ShiftStartCreate(BaseModel):
    """When a 2nd or 3rd Shift started on one machine on one production day.

    `start_hour` is a clock hour on a checkpoint (14 for 2PM, 0 for 12AM), or
    None to go back to starting when the shift before it ends. Whether the
    day still fits is checked by the caller (day_conflict()).
    """

    machine_id: str
    entry_date: date_type
    shift: str
    start_hour: int | None
    entered_by: str = Field(min_length=1, max_length=20)

    @field_validator("shift")
    @classmethod
    def _later_shift(cls, value: str) -> str:
        if value not in SHIFT_ORDER[1:]:
            raise ValueError(f"shift must be one of {SHIFT_ORDER[1:]}")
        return value

    @field_validator("start_hour")
    @classmethod
    def _on_a_checkpoint(cls, value: int | None) -> int | None:
        if value is not None and (value not in range(24) or value % SLOT_HOURS):
            raise ValueError("start_hour must be an even clock hour, 0 to 22")
        return value


# Every machine belongs to exactly one zone, or it would have no board, no
# rounds form and no section on the review pages.
assert sorted(m for zone in DASHBOARD_ZONES.values() for m in zone) == sorted(MACHINE_IDS), (
    "MACHINE_IDS and DASHBOARD_ZONES must list the same machines, each once"
)

# The boards use SHIFT_SLOTS and OEE uses day_plan(); on a day with nothing
# set they must agree.
assert {label: shift["slots"] for label, shift in day_plan({}).items()} == SHIFT_SLOTS, (
    "day_plan({}) must equal SHIFT_SLOTS - TIME_SLOTS or SHIFT_SLOTS was reordered"
)
