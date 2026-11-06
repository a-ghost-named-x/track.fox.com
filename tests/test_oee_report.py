"""Integration check for compute_oee_report(), with the DB reads stubbed out.

Run it from anywhere:

    python tests/test_oee_report.py

Covers the whole report builder at shift grain:

    C1   hits standard, ran clean, zero scrap        -> must score 0.75
    C2   real row: blank tail + 240 min no operator  -> 25.2%, availability 50%
    C3   mid-shift typo the counter recovers from    -> warns, still counts
    C4   LAST reading below an earlier one           -> hard flag, excluded
    C5   production but no downtime record           -> OEE N/A, not 100%
    C6   downtime record but no production           -> no OEE, still in Pareto
    P3   ran, but marked not scheduled               -> absent from rollups

Plus the long-shift cases (shift length is per machine, per day, per shift):

    WS1  12-hour 1st Shift at standard               -> 720 min, 6 slots, 0.75
         ...its 2nd Shift inherits 12h and defaults to NOT scheduled
    WS2  12-hour day with a 6PM-6AM crew ticked on   -> stitched across two dates
    WS3  8-hour 1st Shift, then a 12-hour crew set to start at 6PM
         ...left at the default it would read 2PM-2AM and warn about 4AM
    P1   10-hour 1st Shift at standard               -> 600 min, 5 slots, 0.75

And the production-day rule: C1's 3rd Shift on DAY is DAY 10PM -> DAY_AFTER
6AM, read from DAY_AFTER's night entries.

Then a short Saturday (SAT), short shifts inside their normal 8-hour shifts:

    FM1  6 hours on the 1st Shift at standard       -> 360 min, 0.75
         ...its 2nd and 3rd are a full 8h, not scheduled
    FM2  4 hours from 10AM, some downtime           -> 240 min, still 0.75 OEE
    FM3  6 / 6 / 4, all three crews ticked on       -> 2PM-8PM, 10PM-2AM
    WS4  an ordinary 8h shift, downtime never entered
    WS5  a 6h morning, then an 8h 2nd Shift         -> starts unticked
    WS6  an 8h 1st Shift, then a 4h 2nd crew        -> 2PM-6PM

and a short crew after a long day (TUE):

    C4   12h 1st Shift, then a 4h crew from 6PM     -> 6PM-10PM, 240 min, no 3rd

and start times (LATE), after an 8h 1st Shift:

    WS5  10h 2nd from 2PM, a 6h 3rd crew to 6AM     -> 2PM-12AM, 12AM-6AM, 0.75 each
    WS6  the same with the 3rd's length left unset  -> it fills the rest of the night
    WS4  10h 2nd set to start at 4PM                -> 4PM-2AM, reads the 2AM box
    WS3  a 4PM crew with no start set               -> reads 4PM-12AM, warns about 2AM

and the 7/30-day view over all of it (compute_oee_range), with a day in the
week before for the comparison.

See tests/test_oee_math.py for the unit-level arithmetic. This file checks
aggregation: percentages are never averaged, and A x P x Q must equal OEE
whenever the three factors cover the same machines.
"""
import os
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_DB_PASSWORD", "stub")
try:  # pragma: no cover - environment shim
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

import app.db.entries as entries_mod  # noqa: E402
import app.db.oee as oee  # noqa: E402
from app.models import MACHINE_IDS, SHIFT_SLOTS, TIME_SLOTS, oee_column_days  # noqa: E402

DAY = date(2026, 9, 8)
DAY_BEFORE = date(2026, 9, 7)
DAY_AFTER = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 9, 0)  # two days after, so every slot has elapsed
SAT = date(2026, 9, 12)            # a Saturday: the short-shift cases
SAT_AFTER = date(2026, 9, 13)
NOW_SAT = datetime(2026, 9, 14, 9, 0)
SHIFT = "1st Shift"
S1 = SHIFT_SLOTS[SHIFT]

# Pouch is left out, so it stands in for a machine with no ideal rate yet.
INCREMENTS = {
    "C1": 11700, "C2": 11700, "C3": 11700, "C4": 11700, "C5": 11700,
    "C6": 10800, "C7": 10800, "C8": 10800, "C9": 10800, "C10": 10800, "C11": 10800,
    "C14": 11700, "C15": 11700, "C16": 11700,
    "FM1": 8100, "FM2": 8100, "FM3": 8100,
    "WS1": 9900, "WS2": 9900, "WS3": 9900, "WS4": 9900, "WS5": 9900, "WS6": 9900,
    "P1": 14400, "P2": 16200, "P3": 16200, "P4": 16200,
    "AS1": 2520, "AS2": 2520, "AS3": 2520, "AS4": 2520, "AS5": 2520,
    "AS6": 2250, "AS7": 2250,
}

entries_by_date = {DAY: [], DAY_AFTER: [], SAT: [], SAT_AFTER: []}


def add(machine, values, slots=S1, day=DAY):
    for slot, value in zip(slots, values):
        if value is None:
            continue  # a blank checkpoint is an absent row, not a null one
        entries_by_date[day].append({
            "machine_id": machine, "operator": "Okafor", "time_slot": slot,
            "units_produced": value, "status": ":)", "issue": None,
            "entered_by": "4471", "entry_date": day, "created_at": NOW,
        })


add("C1", [11700, 23400, 35100, 46800])       # textbook
add("C2", [10500, 15750, 15750, None])        # the real C2 row
add("C3", [11700, 23400, 20000, 46800])       # typo that recovers
add("C4", [11700, 23400, 35100, 20000])       # final reading low
add("C5", [11700, 23400, 35100, 46800])       # no downtime record below
add("P3", [16200, 32400, 48600, 64800])       # unscheduled below

# Long shifts. The count keeps running past 2PM: the 4PM and 6PM boxes hold
# the cumulative-since-6AM number.
add("WS1", [9900, 19800, 29700, 39600, 49500, 59400], slots=TIME_SLOTS[:6])
add("P1", [14400, 28800, 43200, 57600, 72000], slots=TIME_SLOTS[:5])
# WS2's night crew: 8PM and 10PM are filed under DAY, 12AM-6AM under DAY_AFTER.
add("WS2", [9900, 19800], slots=["8PM", "10PM"])
add("WS2", [29700, 39600, 49500, 59400], slots=["12AM", "2AM", "4AM", "6AM"], day=DAY_AFTER)
# WS3: an 8-hour 1st Shift, idle 2PM-6PM, then a 12-hour crew from 6PM. Same
# night readings as WS2.
add("WS3", [9900, 19800, 29700, 39600])
add("WS3", [9900, 19800], slots=["8PM", "10PM"])
add("WS3", [29700, 39600, 49500, 59400], slots=["12AM", "2AM", "4AM", "6AM"], day=DAY_AFTER)
# C1's ordinary 3rd Shift on DAY: 10PM DAY -> 6AM DAY_AFTER, filed by the
# rounds under DAY_AFTER.
add("C1", [11700, 23400, 35100, 46800], slots=["12AM", "2AM", "4AM", "6AM"], day=DAY_AFTER)

# Explicitly-set lengths per (machine, shift) for DAY; anything else inherits.
shift_lengths = {
    ("WS1", "1st Shift"): 12,
    ("WS2", "1st Shift"): 12,
    ("WS3", "2nd Shift"): 12,
    ("P1", "1st Shift"): 10,
}

clean = {"note": None, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0}
downtime = {
    ("C1", SHIFT): dict(clean),
    ("C2", SHIFT): {
        "note": "operator moved to WS", "planned_minutes": 0, "unplanned_minutes": 240,
        "reasons": [{"code": "LACK_OPER", "label": "Lack of Operator",
                     "minutes": 240, "is_planned": False}],
    },
    ("C3", SHIFT): dict(clean),
    ("C4", SHIFT): dict(clean),
    # C6 has downtime but no production at all.
    ("C6", SHIFT): {
        "note": None, "planned_minutes": 0, "unplanned_minutes": 75,
        "reasons": [{"code": "EQUIP_FAIL", "label": "Equipment Failure",
                     "minutes": 75, "is_planned": False}],
    },
    ("P3", SHIFT): dict(clean),
    ("WS1", SHIFT): dict(clean),
    ("P1", SHIFT): dict(clean),
    ("WS2", "2nd Shift"): dict(clean),
    ("WS3", SHIFT): dict(clean),
    ("WS3", "2nd Shift"): dict(clean),
    ("C1", "3rd Shift"): dict(clean),
}
scrap = {("C1", SHIFT): 0, ("C2", SHIFT): 250, ("C4", SHIFT): 100,
         ("WS1", SHIFT): 0, ("P1", SHIFT): 0, ("WS2", "2nd Shift"): 0,
         ("WS3", SHIFT): 0, ("WS3", "2nd Shift"): 0, ("C1", "3rd Shift"): 0}
# A 12-hour day's 2nd Shift defaults to not scheduled, so WS2's is ticked on.
schedule = {("P3", SHIFT): False, ("WS2", "2nd Shift"): True, ("WS3", "2nd Shift"): True}
# A downtime record under a shift that doesn't exist (WS3's 2nd Shift ran
# 12h, so there is no 3rd). The range Pareto must ignore it, like the report.
downtime[("WS3", "3rd Shift")] = {
    "note": None, "planned_minutes": 0, "unplanned_minutes": 90,
    "reasons": [{"code": "ROLL_CHANGE", "label": "Roll Change",
                 "minutes": 90, "is_planned": False}],
}

# --- the short Saturday ------------------------------------------------------
# FM's increment is 8,100 per two hours, so standard is 4,050 an hour and the
# ideal 5,400 an hour (WS: 4,950 and 6,600). Every short machine below is
# exactly at standard. A short shift keeps its 8-hour shift's checkpoints.
add("FM1", [8100, 16200, 24300, None], day=SAT)                    # 6AM-12PM
add("FM2", [None, None, 8100, 16200], day=SAT)                     # 10AM-2PM
add("FM3", [8100, 16200, 24300, None], day=SAT)
# The 2nd crew starts at 2PM, counting from zero like any shift change, and
# leaves at 8PM.
add("FM3", [8100, 16200, 24300, None], slots=SHIFT_SLOTS["2nd Shift"], day=SAT)
# A 4-hour 3rd Shift, 10PM-2AM, filed by the rounds under the next morning.
# The count was written into the 4AM box too (unchanged).
add("FM3", [8100, 16200, 16200], slots=["12AM", "2AM", "4AM"], day=SAT_AFTER)
add("WS4", [9900, 19800, 29700, 39600], day=SAT)                   # no downtime entered
# Nothing on the 1st Shift, then a 4-hour crew from 2PM.
add("WS6", [9900, 19800], slots=["4PM", "6PM"], day=SAT)

sat_lengths = {
    ("FM1", "1st Shift"): 6,
    ("FM2", "1st Shift"): 4,
    ("FM3", "1st Shift"): 6, ("FM3", "2nd Shift"): 6, ("FM3", "3rd Shift"): 4,
    ("WS5", "1st Shift"): 6, ("WS5", "2nd Shift"): 8,
    ("WS6", "2nd Shift"): 4,
}
sat_downtime = {
    ("FM1", SHIFT): dict(clean),
    ("FM2", SHIFT): {
        "note": None, "planned_minutes": 0, "unplanned_minutes": 30,
        "reasons": [{"code": "SETUP", "label": "Setup", "minutes": 30, "is_planned": False}],
    },
    # Entered, but FM1's afternoon isn't scheduled, so it stays out of any Pareto.
    ("FM1", "2nd Shift"): {
        "note": None, "planned_minutes": 0, "unplanned_minutes": 50,
        "reasons": [{"code": "DELIVERY", "label": "Delivery", "minutes": 50, "is_planned": False}],
    },
    ("FM3", SHIFT): dict(clean),
    ("FM3", "2nd Shift"): {
        "note": None, "planned_minutes": 0, "unplanned_minutes": 60,
        "reasons": [{"code": "LACK_OPER", "label": "Lack of Operator",
                     "minutes": 60, "is_planned": False}],
    },
    ("FM3", "3rd Shift"): dict(clean),
    ("WS6", "2nd Shift"): dict(clean),
}
sat_scrap = {("FM1", SHIFT): 0, ("FM2", SHIFT): 0, ("FM3", SHIFT): 0,
             ("FM3", "2nd Shift"): 0, ("FM3", "3rd Shift"): 0, ("WS6", "2nd Shift"): 0}
# Shifts at or after a short one default to not scheduled; these crews were
# ticked on.
sat_schedule = {("FM3", "2nd Shift"): True, ("FM3", "3rd Shift"): True,
                ("WS6", "2nd Shift"): True}

# Explicitly-set starts (clock hours) per (machine, shift) for DAY.
shift_starts = {("WS3", "2nd Shift"): 18}

lengths_by_day = {DAY: shift_lengths, SAT: sat_lengths}
starts_by_day = {DAY: shift_starts}
downtime_by_day = {DAY: downtime, SAT: sat_downtime}
scrap_by_day = {DAY: scrap, SAT: sat_scrap}
schedule_by_day = {DAY: schedule, SAT: sat_schedule}


def by_range(table, start, end):
    """A per-day stub table as the range readers return it: keyed with the date."""
    return {
        (machine, day, shift): value
        for day, rows in table.items() if start <= day <= end
        for (machine, shift), value in rows.items()
    }


# Kept before stubbing, so the one-day wrappers over the range readers can be
# checked at the end.
real_downtime_for_date = oee.get_latest_downtime_for_date
real_schedule_for_date = oee.get_schedule_for_date
real_lengths_for_date = oee.get_shift_lengths
real_starts_for_date = oee.get_shift_starts

entries_mod.get_latest_entries_for_date = lambda d: list(entries_by_date.get(d, []))
entries_mod.get_readings_for_range = lambda start, end: {
    (row["machine_id"], day, row["time_slot"]): row["units_produced"]
    for day, rows in entries_by_date.items() if start <= day <= end
    for row in rows
}
oee.get_latest_scrap_for_date = lambda d: dict(scrap_by_day.get(d, {}))
oee.get_scrap_for_range = lambda s, e: by_range(scrap_by_day, s, e)
oee.get_oee_tracking_start = lambda: min((d for d, rows in downtime_by_day.items() if rows), default=None)
oee.get_latest_downtime_for_date = lambda d: dict(downtime_by_day.get(d, {}))
oee.get_schedule_for_date = lambda d: dict(schedule_by_day.get(d, {}))
oee.get_shift_lengths = lambda d: dict(lengths_by_day.get(d, {}))
oee.get_shift_lengths_for_range = lambda s, e: by_range(lengths_by_day, s, e)
oee.get_shift_starts = lambda d: dict(starts_by_day.get(d, {}))
oee.get_shift_starts_for_range = lambda s, e: by_range(starts_by_day, s, e)
oee.get_downtime_for_range = lambda s, e: by_range(downtime_by_day, s, e)
oee.get_schedule_for_range = lambda s, e: by_range(schedule_by_day, s, e)
oee.get_ideal_rates = lambda: {m: inc / 1.5 for m, inc in INCREMENTS.items()}
oee.get_shift_standards = lambda: {
    (m, s): inc * 4 for m, inc in INCREMENTS.items() for s in SHIFT_SLOTS
}

report = oee.compute_oee_report(DAY, now=NOW)
shift = report["shifts"][SHIFT]
M = shift["machines"]

failures = []


def check(label, got, want, tol=1e-9):
    ok = (got is None and want is None) or (
        isinstance(got, (int, float)) and isinstance(want, (int, float))
        and not isinstance(got, bool) and abs(got - want) <= tol
    ) or got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


print("\n== C1: at standard, ran clean, zero scrap ==")
c1 = M["C1"]["shift"]
check("oee", c1["oee"], 0.75)
check("availability", c1["availability"], 1.0)
check("quality", c1["quality"], 1.0)
check("A x P x Q == oee", c1["oee_from_factors"], 0.75)
check("good is the last reading", c1["good"], 46800)
check("ppt is the whole shift", c1["ppt_minutes"], 480)
check("no flags", M["C1"]["flags"], [])

print("\n== C2: the real row — blank tail, 240 min no operator ==")
c2 = M["C2"]["shift"]
check("good is the last READING, not the last entry", c2["good"], 15750)
check("oee", c2["oee"], 15750 / (130 * 480))
check("availability is 240 of 480", c2["availability"], 0.5)
check("quality", c2["quality"], 15750 / 16000)
check("A x P x Q == oee", c2["oee_from_factors"], c2["oee"])
check("the blank tail did not shorten the shift", c2["ppt_minutes"], 480)

print("\n== C3: mid-shift typo the counter recovers from ==")
check("warns", M["C3"]["warnings"], ["units_went_backwards"])
check("but is NOT excluded", M["C3"]["shift"] is not None, True)
check("shift total is still correct", M["C3"]["shift"]["good"], 46800)

print("\n== C4: last reading below an earlier one ==")
check("hard flag", M["C4"]["flags"], ["final_reading_low"])
check("excluded from OEE", M["C4"]["shift"], None)
check("scrap is still shown", M["C4"]["scrap"], 100)

print("\n== C5: production but nobody entered downtime ==")
check("no OEE rather than a flattering 100% availability", M["C5"]["shift"], None)
check("has_production", M["C5"]["has_production"], True)
check("has_downtime", M["C5"]["has_downtime"], False)
check("no flags — this is missing data, not bad data", M["C5"]["flags"], [])

print("\n== C6: downtime recorded but no production ==")
check("no OEE", M["C6"]["shift"], None)
check("downtime is still carried", M["C6"]["has_downtime"], True)
check("and its reasons survive for the Pareto", len(M["C6"]["reasons"]), 1)

print("\n== PB1: no ideal rate ==")
check("flagged, so the banner says why", M["PB1"]["flags"], ["no_ideal_rate"])
check("no OEE", M["PB1"]["shift"], None)

print("\n== P3: not scheduled ==")
check("scheduled false", M["P3"]["scheduled"], False)
check("still computed individually", M["P3"]["shift"] is not None, True)

print("\n== WS1: 12-hour 1st Shift at standard ==")
ws1 = M["WS1"]
check("shift_hours", ws1["shift_hours"], 12)
check("span", ws1["span"], "6AM-6PM")
check("six checkpoints", [c["slot"] for c in ws1["checkpoints"]],
      ["8AM", "10AM", "12PM", "2PM", "4PM", "6PM"])
check("ppt is 720", ws1["shift"]["ppt_minutes"], 720)
check("good is the 6PM reading", ws1["shift"]["good"], 59400)
check("standard is scaled to 12 hours (39,600 x 1.5)", ws1["shift"]["standard"], 59400)
check("pct of standard", ws1["shift"]["pct_of_standard"], 1.0)
check("oee is exactly 0.75 — no false 'beat the maximum'", ws1["shift"]["oee"], 0.75)
check("no warnings", ws1["warnings"], [])
check("A x P x Q == oee", ws1["shift"]["oee_from_factors"], 0.75)

print("\n== WS1's 2nd Shift on a 12-hour day defaults to NOT scheduled ==")
ws1_2nd = report["shifts"]["2nd Shift"]["machines"]["WS1"]
check("the shift exists (a 6PM crew is possible)", ws1_2nd["shift_exists"], True)
check("but is not scheduled unless ticked", ws1_2nd["scheduled"], False)
check("span is the night", ws1_2nd["span"], "6PM-6AM")

print("\n== WS2: a 6PM-6AM crew, ticked on, stitched across two dates ==")
ws2_2nd = report["shifts"]["2nd Shift"]["machines"]["WS2"]
check("scheduled because the box was ticked", ws2_2nd["scheduled"], True)
check("checkpoints span midnight",
      [(c["slot"], c["date"]) for c in ws2_2nd["checkpoints"]],
      [("8PM", "2026-09-08"), ("10PM", "2026-09-08"), ("12AM", "2026-09-09"),
       ("2AM", "2026-09-09"), ("4AM", "2026-09-09"), ("6AM", "2026-09-09")])
check("good is the 6AM reading from the NEXT date", ws2_2nd["shift"]["good"], 59400)
check("ppt is 720", ws2_2nd["shift"]["ppt_minutes"], 720)
check("oee", ws2_2nd["shift"]["oee"], 0.75)
check("it is in the 2nd Shift rollup",
      report["shifts"]["2nd Shift"]["rollup"]["machines"], 2)

print("\n== WS3, the manager's case: 8h 1st Shift, then a 12h crew set to start at 6PM ==")
check("1st Shift is an ordinary 8-hour one", M["WS3"]["shift_hours"], 8)
check("1st Shift span", M["WS3"]["span"], "6AM-2PM")
check("1st Shift oee", M["WS3"]["shift"]["oee"], 0.75)
ws3_2nd = report["shifts"]["2nd Shift"]["machines"]["WS3"]
check("2nd Shift is 12 hours", ws3_2nd["shift_hours"], 12)
check("2nd Shift is 6PM-6AM, not 2PM-2AM", ws3_2nd["span"], "6PM-6AM")
check("2nd Shift stitched across midnight", ws3_2nd["shift"]["good"], 59400)
check("2nd Shift oee", ws3_2nd["shift"]["oee"], 0.75)
ws3_3rd = report["shifts"]["3rd Shift"]["machines"]["WS3"]
check("no 3rd Shift that night", ws3_3rd["shift_exists"], False)
check("so it is not scheduled", ws3_3rd["scheduled"], False)
check("and says who covered the night", ws3_3rd["covered_by"],
      {"shift": "2nd Shift", "hours": 12, "span": "6PM-6AM", "window_span": "6PM-6AM"})
check("3rd Shift completeness leaves it out (WS1, WS2, WS3 have none, P1's is unticked)",
      report["shifts"]["3rd Shift"]["completeness"]["machines"], len(MACHINE_IDS) - 4)
check("no warning: the 4PM and 6PM boxes before the crew are blank",
      (M["WS3"]["warnings"], ws3_2nd["warnings"]), ([], []))

del shift_starts[("WS3", "2nd Shift")]
unset = oee.compute_oee_report(DAY, now=NOW)["shifts"]["2nd Shift"]["machines"]["WS3"]
check("with no start set, a 12h 2nd Shift after an 8h day is 2PM-2AM", unset["span"], "2PM-2AM")
check("...which stops at the 2AM reading", unset["shift"]["good"], 39600)
check("...and says the crew was still counting at 4AM",
      (unset["warnings"], unset["late_reading"]),
      (["reading_after_shift"],
       {"slot": "4AM", "date": "2026-09-09", "reading": 49500, "shift_ended": "2AM"}))
shift_starts[("WS3", "2nd Shift")] = 18

print("\n== the production day: C1's 3rd Shift is DAY 10PM -> DAY_AFTER 6AM ==")
c1_3rd = report["shifts"]["3rd Shift"]["machines"]["C1"]
check("exists", c1_3rd["shift_exists"], True)
check("checkpoints carry the next calendar date",
      [(c["slot"], c["date"]) for c in c1_3rd["checkpoints"]],
      [("12AM", "2026-09-09"), ("2AM", "2026-09-09"), ("4AM", "2026-09-09"), ("6AM", "2026-09-09")])
check("good is the 6AM reading from DAY_AFTER's entries", c1_3rd["shift"]["good"], 46800)
check("oee", c1_3rd["shift"]["oee"], 0.75)
check("its downtime came from DAY's rows (filed under the day it started)",
      c1_3rd["has_downtime"], True)

print("\n== P1: 10-hour 1st Shift at standard ==")
p1 = M["P1"]
check("five checkpoints", len(p1["checkpoints"]), 5)
check("span", p1["span"], "6AM-4PM")
check("ppt is 600", p1["shift"]["ppt_minutes"], 600)
check("standard is 57,600 x 1.25", p1["shift"]["standard"], 72000)
check("oee is exactly 0.75", p1["shift"]["oee"], 0.75)
check("its 2nd Shift inherits 10h: 4PM-2AM",
      report["shifts"]["2nd Shift"]["machines"]["P1"]["span"], "4PM-2AM")
p1_3rd = report["shifts"]["3rd Shift"]["machines"]["P1"]
check("a 3rd fits after it, 2AM-6AM, but starts unticked",
      (p1_3rd["shift_exists"], p1_3rd["span"], p1_3rd["shift_hours"], p1_3rd["scheduled"]),
      (True, "2AM-6AM", 4, False))

print("\n== ordinary machines are untouched by all of this ==")
check("C1 still has four checkpoints", len(M["C1"]["checkpoints"]), 4)
check("C1 shift_hours", M["C1"]["shift_hours"], 8)
check("C1 span", M["C1"]["span"], "6AM-2PM")
check("C1 3rd Shift exists and is scheduled",
      report["shifts"]["3rd Shift"]["machines"]["C1"]["scheduled"], True)
check("C2 3rd Shift exists, unentered, still 8h",
      report["shifts"]["3rd Shift"]["machines"]["C2"]["shift_hours"], 8)

print("\n== rollup ==")
rollup = shift["rollup"]
# C1, C2, C3, WS1, WS3, P1 countable and scheduled. C4 flagged, C5/C6
# incomplete, P3 excluded.
check("machines counted", rollup["machines"], 6)
check("P3's 64,800 is not in the rollup",
      rollup["good"], 46800 + 15750 + 46800 + 59400 + 39600 + 72000)
check("scrap subset is narrower (C3 has none)", rollup["split_machines"], 5)
check("identity withheld on mixed populations", rollup["oee_from_factors"], None)
check("a 12-hour machine contributes 720 minutes of PPT, not 480",
      rollup["ppt_minutes"], 480 * 4 + 720 + 600)
check("2nd Shift rollup: WS2 and WS3's night crews", report["shifts"]["2nd Shift"]["rollup"]["machines"], 2)

print("\n== per-zone rollups come from the server ==")
zones = shift["zones"]
check("b3 holds the C machines", zones["b3"]["machines"], 3)
check("b3 oee is component-summed, not averaged",
      zones["b3"]["oee"], (46800 + 15750 + 46800) / (130 * 480 * 3))
check("b4 holds P1 only — P3 is unscheduled", zones["b4"]["machines"], 1)
check("ws holds WS1 and WS3 (WS2 only ran nights)", zones["ws"]["machines"], 2)

print("\n== pareto ==")
pareto = shift["pareto"]
check("ranked by minutes", [p["code"] for p in pareto], ["LACK_OPER", "EQUIP_FAIL"])
check("LACK_OPER minutes", pareto[0]["minutes"], 240)
check("C6 contributes despite having no OEE", pareto[1]["minutes"], 75)
check("share of unplanned", pareto[0]["pct_of_unplanned"], 240 / 315)

print("\n== completeness is per column, counted in machines ==")
c = shift["completeness"]
check("scheduled machines", c["machines"], len(MACHINE_IDS) - 1)  # all but unscheduled P3
check("production entered", c["production"], 8)         # C1-C5, WS1, WS3, P1 (P3 excluded)
check("downtime entered", c["downtime"], 8)             # C1-C4, C6, WS1, WS3, P1
check("scrap entered", c["scrap"], 6)                   # C1, C2, C4, WS1, WS3, P1
check("other-length machines on 1st Shift", c["other_length_machines"], 3)  # WS1, WS2, P1
check("slots_expected is the 8-hour view", c["slots_expected"], 4)

print("\n== payload is JSON-serialisable (the API returns it directly) ==")
import json  # noqa: E402
try:
    json.dumps(report)
    print("  PASS  json.dumps succeeded")
except TypeError as exc:
    print(f"  FAIL  json.dumps: {exc}")
    failures.append("json serialisable")

print("\n== an in-progress shift is not charged for hours that haven't happened ==")
mid = oee.compute_oee_report(DAY, now=datetime(2026, 9, 8, 11, 30))
mid_c1 = mid["shifts"][SHIFT]["machines"]["C1"]
check("only two slots elapsed", mid["shifts"][SHIFT]["completeness"]["slots_expected"], 2)
check("ppt covers the elapsed part only", mid_c1["shift"]["ppt_minutes"], 240)
check("good is the last ELAPSED reading", mid_c1["shift"]["good"], 23400)

# 4:30PM: an 8-hour 1st Shift is over, a 12-hour one is five slots in.
late = oee.compute_oee_report(DAY, now=datetime(2026, 9, 8, 16, 30))
check("C1's shift is complete", late["shifts"][SHIFT]["machines"]["C1"]["elapsed_count"], 4)
check("WS1 has five of six slots", late["shifts"][SHIFT]["machines"]["WS1"]["elapsed_count"], 5)
check("and 600 minutes so far", late["shifts"][SHIFT]["machines"]["WS1"]["shift"]["ppt_minutes"], 600)
check("good is the 4PM reading", late["shifts"][SHIFT]["machines"]["WS1"]["shift"]["good"], 49500)

# 1AM the next morning: WS2's night crew is three checkpoints in, the third
# of which is on the new calendar date.
night = oee.compute_oee_report(DAY, now=datetime(2026, 9, 9, 1, 0))
ws2_night = night["shifts"]["2nd Shift"]["machines"]["WS2"]
check("8PM, 10PM and 12AM elapsed", ws2_night["elapsed_count"], 3)
check("good is the 12AM reading from the next date", ws2_night["shift"]["good"], 29700)
check("ppt is 360", ws2_night["shift"]["ppt_minutes"], 360)

print("\n== SHORT SHIFTS: inside the normal 8-hour shift, as geometry ==")
from app.models import (  # noqa: E402
    SHORT_SHIFT_HOURS, day_conflict, day_plan, default_scheduled, shift_span, window_span,
)
check("a short shift is 2, 4 or 6 hours, never odd", SHORT_SHIFT_HOURS, [2, 4, 6])
check("on an ordinary day 2, 4 and 6 hours keep the 8-hour checkpoints",
      [{s: day_plan({s: h})[s]["slots"] for s in SHIFT_SLOTS} for h in (2, 4, 6)],
      [SHIFT_SLOTS] * 3)
check("a short 2nd Shift starts at 2PM", shift_span("2nd Shift", day_plan({"2nd Shift": 6})),
      "2PM-8PM")
check("a short 3rd Shift starts at 10PM", shift_span("3rd Shift", day_plan({"3rd Shift": 4})),
      "10PM-2AM")
check("a short 1st Shift has no fixed start (6, 8 or 10AM)",
      shift_span("1st Shift", day_plan({"1st Shift": 4})), "within 6AM-2PM")
check("...inside the 6AM-2PM shift", window_span("1st Shift", day_plan({"1st Shift": 4})),
      "6AM-2PM")
check("a later shift inherits a full 8 hours, not the typed hours",
      [p["hours"] for p in day_plan({"1st Shift": 4}).values()], [4, 8, 8])
check("6 then 6 then 4 is a valid day",
      day_conflict(day_plan({"1st Shift": 6, "2nd Shift": 6, "3rd Shift": 4})), None)
check("an 8h 1st Shift, then a short 2nd, is a valid day",
      day_conflict(day_plan({"2nd Shift": 4})), None)
check("a short 2nd Shift after a 10h 1st is a valid day, inside 4PM-2AM",
      (day_conflict(day_plan({"1st Shift": 10, "2nd Shift": 4})),
       window_span("2nd Shift", day_plan({"1st Shift": 10, "2nd Shift": 4}))), (None, "4PM-2AM"))
check("a short 1st Shift defaults to scheduled",
      default_scheduled("1st Shift", day_plan({"1st Shift": 4})), True)
check("after a short 1st, the 8h 2nd and 3rd default to NOT scheduled",
      [default_scheduled(s, day_plan({"1st Shift": 4})) for s in ("2nd Shift", "3rd Shift")],
      [False, False])
check("a short 2nd after an 8h 1st: only the 1st defaults to scheduled",
      [default_scheduled(s, day_plan({"2nd Shift": 4})) for s in SHIFT_SLOTS],
      [True, False, False])
check("a short 3rd alone leaves the 1st and 2nd scheduled",
      [default_scheduled(s, day_plan({"3rd Shift": 4})) for s in SHIFT_SLOTS],
      [True, True, False])

sat = oee.compute_oee_report(SAT, now=NOW_SAT)
SM = sat["shifts"][SHIFT]["machines"]

print("\n== FM1: 6 hours on the 1st Shift, at standard ==")
fm1 = SM["FM1"]
check("shift_hours", fm1["shift_hours"], 6)
check("span", fm1["span"], "within 6AM-2PM")
check("the 1st Shift's four checkpoints", [c["slot"] for c in fm1["checkpoints"]], S1)
check("ppt is 360", fm1["shift"]["ppt_minutes"], 360)
check("standard scaled to 6 hours (32,400 x 6/8)", fm1["shift"]["standard"], 24300)
check("good is the 12PM reading, the 2PM box being blank", fm1["shift"]["good"], 24300)
check("oee is exactly 0.75 — judged on 6 hours, not 8", fm1["shift"]["oee"], 0.75)
check("A x P x Q == oee", fm1["shift"]["oee_from_factors"], 0.75)
fm1_2nd = sat["shifts"]["2nd Shift"]["machines"]["FM1"]
check("its 2nd Shift is a full 8h, 2PM-10PM", (fm1_2nd["shift_hours"], fm1_2nd["span"]),
      (8, "2PM-10PM"))
check("...and not scheduled unless ticked", fm1_2nd["scheduled"], False)
check("...which the page can explain", fm1_2nd["short_day"], True)
fm1_3rd = sat["shifts"]["3rd Shift"]["machines"]["FM1"]
check("its 3rd Shift is 10PM-6AM, not scheduled",
      (fm1_3rd["shift_exists"], fm1_3rd["span"], fm1_3rd["scheduled"]), (True, "10PM-6AM", False))

print("\n== FM2: 4 hours from 10AM, with 30 minutes of setup ==")
fm2 = SM["FM2"]
check("span gives no start time", fm2["span"], "within 6AM-2PM")
check("window is the whole shift", fm2["window_span"], "6AM-2PM")
check("four checkpoints (the 8-hour shift's)", len(fm2["checkpoints"]), 4)
check("ppt is 240, not 480", fm2["shift"]["ppt_minutes"], 240)
check("shift_minutes is 240", fm2["shift_minutes"], 240)
check("availability is 210 of 240", fm2["shift"]["availability"], 210 / 240)
check("standard scaled to 4 hours", fm2["shift"]["standard"], 16200)
check("good is the 2PM reading; the blank 8AM and 10AM were zero", fm2["shift"]["good"], 16200)
check("oee", fm2["shift"]["oee"], 0.75)
check("no warnings", fm2["warnings"], [])

print("\n== FM3: a Saturday with all three crews ==")
fm3_2nd = sat["shifts"]["2nd Shift"]["machines"]["FM3"]
check("6-hour 2nd Shift is 2PM-8PM", fm3_2nd["span"], "2PM-8PM")
check("2nd Shift checkpoints are the rounds' own",
      [c["slot"] for c in fm3_2nd["checkpoints"]], SHIFT_SLOTS["2nd Shift"])
check("good is the 8PM reading, counted from zero at 2PM", fm3_2nd["shift"]["good"], 24300)
check("ppt is 360", fm3_2nd["shift"]["ppt_minutes"], 360)
check("2nd Shift oee", fm3_2nd["shift"]["oee"], 0.75)
fm3_3rd = sat["shifts"]["3rd Shift"]["machines"]["FM3"]
check("4-hour 3rd Shift is 10PM-2AM", fm3_3rd["span"], "10PM-2AM")
check("its checkpoints are filed under the next morning",
      [(c["slot"], c["date"]) for c in fm3_3rd["checkpoints"]],
      [(s, "2026-09-13") for s in SHIFT_SLOTS["3rd Shift"]])
check("good is the last reading", fm3_3rd["shift"]["good"], 16200)
check("ppt is 240", fm3_3rd["shift"]["ppt_minutes"], 240)
check("standard scaled to 4 hours", fm3_3rd["shift"]["standard"], 16200)
check("oee", fm3_3rd["shift"]["oee"], 0.75)
check("3rd Shift rollup counts FM3 only",
      sat["shifts"]["3rd Shift"]["rollup"]["machines"], 1)

print("\n== WS5: a 6-hour morning, then an 8h 2nd Shift ==")
ws5_2nd = sat["shifts"]["2nd Shift"]["machines"]["WS5"]
check("the 2nd Shift is 2PM-10PM", ws5_2nd["span"], "2PM-10PM")
check("and starts unticked after the short morning", ws5_2nd["scheduled"], False)
check("the 3rd follows it: 10PM-6AM",
      sat["shifts"]["3rd Shift"]["machines"]["WS5"]["span"], "10PM-6AM")

print("\n== WS6: an ordinary 1st Shift, then a 4-hour 2nd crew ==")
ws6_2nd = sat["shifts"]["2nd Shift"]["machines"]["WS6"]
check("2PM-6PM", ws6_2nd["span"], "2PM-6PM")
check("good is the 6PM reading", ws6_2nd["shift"]["good"], 19800)
check("ppt is 240", ws6_2nd["shift"]["ppt_minutes"], 240)
check("oee", ws6_2nd["shift"]["oee"], 0.75)
check("its 1st Shift is ordinary", (SM["WS6"]["shift_hours"], SM["WS6"]["scheduled"]), (8, True))
check("its 3rd Shift starts unticked",
      sat["shifts"]["3rd Shift"]["machines"]["WS6"]["scheduled"], False)

print("\n== the Saturday rollup and completeness ==")
sat_rollup = sat["shifts"][SHIFT]["rollup"]
check("1st Shift rollup: FM1, FM2, FM3", sat_rollup["machines"], 3)
check("with 360 + 240 + 360 minutes of PPT", sat_rollup["ppt_minutes"], 960)
check("other-length machines on 1st Shift (FM1, FM2, FM3, WS5)",
      sat["shifts"][SHIFT]["completeness"]["other_length_machines"], 4)
check("2nd Shift rollup: FM3 and WS6", sat["shifts"]["2nd Shift"]["rollup"]["machines"], 2)

print("\n== a short shift in progress ==")
mid_sat = oee.compute_oee_report(SAT, now=datetime(2026, 9, 12, 9, 0))
check("at 9AM FM1 has 120 minutes so far",
      mid_sat["shifts"][SHIFT]["machines"]["FM1"]["shift"]["ppt_minutes"], 120)
after_sat = oee.compute_oee_report(SAT, now=datetime(2026, 9, 12, 15, 0))
check("at 3PM it is capped at its 6 hours, though all four slots have closed",
      after_sat["shifts"][SHIFT]["machines"]["FM1"]["shift"]["ppt_minutes"], 360)

# --- a short crew after a 12-hour day -----------------------------------------
# C4's real night: a 12h 1st Shift at standard, then a new operator from 6PM
# to 10PM and nobody after. The rounds have 8PM and 10PM, the 12AM-6AM boxes
# are blank. After the period tests' last day, so it can't change them.
TUE = date(2026, 9, 15)
TUE_AFTER = date(2026, 9, 16)
NOW_TUE = datetime(2026, 9, 17, 9, 0)
entries_by_date[TUE] = []
entries_by_date[TUE_AFTER] = []
add("C4", [11700, 23400, 35100, 46800, 58500, 70200], slots=TIME_SLOTS[:6], day=TUE)
add("C4", [9000, 16000], slots=["8PM", "10PM"], day=TUE)
lengths_by_day[TUE] = {("C4", "1st Shift"): 12, ("C4", "2nd Shift"): 4}
downtime_by_day[TUE] = {
    ("C4", SHIFT): dict(clean),
    ("C4", "2nd Shift"): {
        "note": None, "planned_minutes": 0, "unplanned_minutes": 80,
        "reasons": [{"code": "ROLL_CHANGE", "label": "Roll Change",
                     "minutes": 80, "is_planned": False}],
    },
}
scrap_by_day[TUE] = {("C4", SHIFT): 0, ("C4", "2nd Shift"): 1083}
schedule_by_day[TUE] = {("C4", "2nd Shift"): True}

print("\n== C4: a 12h day, then a 4-hour crew from 6PM ==")
tue = oee.compute_oee_report(TUE, now=NOW_TUE)
c4_1st = tue["shifts"][SHIFT]["machines"]["C4"]
c4_2nd = tue["shifts"]["2nd Shift"]["machines"]["C4"]
check("the 1st Shift is an ordinary 12h one", c4_1st["span"], "6AM-6PM")
check("...at standard", c4_1st["shift"]["oee"], 0.75)
check("the night crew is 4h, 6PM-10PM", (c4_2nd["shift_hours"], c4_2nd["span"]), (4, "6PM-10PM"))
check("inside the 6PM-6AM night", (c4_2nd["window_span"], c4_2nd["window_hours"]), ("6PM-6AM", 12))
check("it owns the night's checkpoints, the 12AM-6AM ones from the next date",
      [(c["slot"], c["date"]) for c in c4_2nd["checkpoints"]],
      [("8PM", "2026-09-15"), ("10PM", "2026-09-15"), ("12AM", "2026-09-16"),
       ("2AM", "2026-09-16"), ("4AM", "2026-09-16"), ("6AM", "2026-09-16")])
check("good is the 10PM reading; the blanks after it are unchanged", c4_2nd["shift"]["good"], 16000)
check("ppt is 240, not 720", c4_2nd["shift"]["ppt_minutes"], 240)
check("run is 160", c4_2nd["shift"]["run_minutes"], 160)
check("the standard is half the 8-hour one", c4_2nd["shift"]["standard"], 23400)
check("oee is good / (ideal x 240 minutes)", c4_2nd["shift"]["oee"], 16000 / (130 * 240))
check("nothing flagged", c4_2nd["flags"], [])
check("the operator is the night crew's", c4_2nd["operator"], "Okafor")
c4_3rd = tue["shifts"]["3rd Shift"]["machines"]["C4"]
check("there is no 3rd Shift that night", (c4_3rd["shift_exists"], c4_3rd["scheduled"]), (False, False))
check("...covered by the 2nd Shift's 6PM-6AM",
      c4_3rd["covered_by"],
      {"shift": "2nd Shift", "hours": 4, "span": "6PM-10PM", "window_span": "6PM-6AM"})
check("the 2nd Shift rollup counts C4 at 240 minutes",
      (tue["shifts"]["2nd Shift"]["rollup"]["machines"],
       tue["shifts"]["2nd Shift"]["rollup"]["ppt_minutes"]), (1, 240))
mid_night = oee.compute_oee_report(TUE, now=datetime(2026, 9, 15, 21, 0))
check("at 9PM the crew has 120 minutes so far",
      mid_night["shifts"]["2nd Shift"]["machines"]["C4"]["shift"]["ppt_minutes"], 120)
late_night = oee.compute_oee_report(TUE, now=datetime(2026, 9, 16, 3, 0))
check("at 3AM it is capped at 240, though four slots have closed",
      late_night["shifts"]["2nd Shift"]["machines"]["C4"]["shift"]["ppt_minutes"], 240)
del schedule_by_day[TUE][("C4", "2nd Shift")]
unticked = oee.compute_oee_report(TUE, now=NOW_TUE)["shifts"]["2nd Shift"]["machines"]["C4"]
check("left unticked, a short night crew is not scheduled", unticked["scheduled"], False)
schedule_by_day[TUE][("C4", "2nd Shift")] = True
json.dumps(tue)

# --- start times ---------------------------------------------------------------
# An 8h 1st Shift at standard on every machine below, then a 10h 2nd Shift.
# After the period tests' last day, so it can't change them.
LATE = date(2026, 9, 19)
LATE_AFTER = date(2026, 9, 20)
NOW_LATE = datetime(2026, 9, 21, 9, 0)
entries_by_date[LATE] = []
entries_by_date[LATE_AFTER] = []
NIGHT_SLOTS = ["12AM", "2AM", "4AM", "6AM"]
for machine in ("WS3", "WS4", "WS5", "WS6"):
    add(machine, [9900, 19800, 29700, 39600], day=LATE)
# WS5 and WS6, the floor's case: the 2nd crew from 2PM to 12AM, then a 3rd
# crew from 12AM to 6AM, each counting from zero.
for machine in ("WS5", "WS6"):
    add(machine, [9900, 19800, 29700, 39600], slots=["4PM", "6PM", "8PM", "10PM"], day=LATE)
    add(machine, [49500, 9900, 19800, 29700], slots=NIGHT_SLOTS, day=LATE_AFTER)
# WS4 and WS3: a 4PM-2AM crew and nobody after it. WS4's start is set, WS3's
# was forgotten.
for machine in ("WS3", "WS4"):
    add(machine, [9900, 19800, 29700], slots=["6PM", "8PM", "10PM"], day=LATE)
    add(machine, [39600, 49500], slots=["12AM", "2AM"], day=LATE_AFTER)
lengths_by_day[LATE] = {
    ("WS5", "2nd Shift"): 10, ("WS5", "3rd Shift"): 6,
    ("WS6", "2nd Shift"): 10,
    ("WS4", "2nd Shift"): 10,
    ("WS3", "2nd Shift"): 10,
}
starts_by_day[LATE] = {("WS4", "2nd Shift"): 16}
downtime_by_day[LATE] = {
    (m, s): dict(clean)
    for m in ("WS3", "WS4", "WS5", "WS6") for s in ("1st Shift", "2nd Shift", "3rd Shift")
}
scrap_by_day[LATE] = {}
# A 10h 2nd Shift starts unticked; so does a 3rd after it.
schedule_by_day[LATE] = {
    ("WS5", "2nd Shift"): True, ("WS5", "3rd Shift"): True,
    ("WS6", "2nd Shift"): True, ("WS6", "3rd Shift"): True,
    ("WS4", "2nd Shift"): True, ("WS3", "2nd Shift"): True,
}
late = oee.compute_oee_report(LATE, now=NOW_LATE)
LM2 = late["shifts"]["2nd Shift"]["machines"]
LM3 = late["shifts"]["3rd Shift"]["machines"]

print("\n== WS5: a 10h 2nd Shift from 2PM, then a 3rd crew to 6AM ==")
check("the 2nd Shift is 2PM-12AM", (LM2["WS5"]["span"], LM2["WS5"]["shift_hours"]), ("2PM-12AM", 10))
check("...its checkpoints 4PM to 12AM, the last from the next date",
      [(c["slot"], c["date"]) for c in LM2["WS5"]["checkpoints"]],
      [("4PM", "2026-09-19"), ("6PM", "2026-09-19"), ("8PM", "2026-09-19"),
       ("10PM", "2026-09-19"), ("12AM", "2026-09-20")])
check("good is the 12AM reading, not the 3rd crew's 2AM one", LM2["WS5"]["shift"]["good"], 49500)
check("ppt is 600", LM2["WS5"]["shift"]["ppt_minutes"], 600)
check("oee is exactly 0.75", LM2["WS5"]["shift"]["oee"], 0.75)
check("no flags or warnings", (LM2["WS5"]["flags"], LM2["WS5"]["warnings"]), ([], []))
check("the 3rd Shift exists, 12AM-6AM, 6h",
      (LM3["WS5"]["shift_exists"], LM3["WS5"]["span"], LM3["WS5"]["shift_hours"]),
      (True, "12AM-6AM", 6))
check("...from the 2AM, 4AM and 6AM boxes", [c["slot"] for c in LM3["WS5"]["checkpoints"]],
      ["2AM", "4AM", "6AM"])
check("...good is the 6AM reading", LM3["WS5"]["shift"]["good"], 29700)
check("...ppt is 360, the standard scaled to 6h",
      (LM3["WS5"]["shift"]["ppt_minutes"], LM3["WS5"]["shift"]["standard"]), (360, 29700))
check("...oee is exactly 0.75", LM3["WS5"]["shift"]["oee"], 0.75)
check("WS6, with the 3rd's length unset, comes out the same",
      (LM3["WS6"]["shift_hours"], LM3["WS6"]["span"], LM3["WS6"]["shift"]["oee"]),
      (6, "12AM-6AM", 0.75))

print("\n== WS4: a 10h 2nd Shift set to start at 4PM ==")
check("4PM-2AM", LM2["WS4"]["span"], "4PM-2AM")
check("the start is marked as set", (LM2["WS4"]["start_set"], LM2["WS5"]["start_set"]), (True, False))
check("good is the 2AM reading", LM2["WS4"]["shift"]["good"], 49500)
check("oee is exactly 0.75", LM2["WS4"]["shift"]["oee"], 0.75)
check("no warnings", LM2["WS4"]["warnings"], [])
check("a 3rd fits after it, 2AM-6AM, unticked",
      (LM3["WS4"]["span"], LM3["WS4"]["scheduled"]), ("2AM-6AM", False))

print("\n== WS3: the same 4PM crew with its start forgotten ==")
check("read as 2PM-12AM", LM2["WS3"]["span"], "2PM-12AM")
check("so it stops at the 12AM reading", LM2["WS3"]["shift"]["good"], 39600)
check("...and is still counted", LM2["WS3"]["flags"], [])
check("but warns about the higher 2AM reading", LM2["WS3"]["warnings"], ["reading_after_shift"])
check("...naming the box", LM2["WS3"]["late_reading"],
      {"slot": "2AM", "date": "2026-09-20", "reading": 49500, "shift_ended": "12AM"})
check("WS5's 2AM box belongs to its ticked 3rd crew, so no warning there",
      LM2["WS5"]["late_reading"], None)
schedule_by_day[LATE][("WS3", "2nd Shift")] = False
unticked_ws3 = oee.compute_oee_report(LATE, now=NOW_LATE)["shifts"]["2nd Shift"]["machines"]["WS3"]
check("a shift that isn't scheduled doesn't warn", unticked_ws3["warnings"], [])
schedule_by_day[LATE][("WS3", "2nd Shift")] = True
json.dumps(late)

# --- 7 and 30 days ------------------------------------------------------------
# Two more days. PREV is in the week before SAT's week, so it's what the
# 7-day view compares against: C1, C2 and C3 at standard, ran clean.
PREV = date(2026, 9, 1)
entries_by_date[PREV] = []
for machine in ("C1", "C2", "C3"):
    add(machine, [11700, 23400, 35100, 46800], day=PREV)
downtime_by_day[PREV] = {(m, SHIFT): dict(clean) for m in ("C1", "C2", "C3")}
scrap_by_day[PREV] = {(m, SHIFT): 0 for m in ("C1", "C2", "C3")}
# THU is in SAT's week: C3 reported two shifts with no downtime entered, so
# the week has one counted C3 shift (DAY's) and two left out.
THU = date(2026, 9, 10)
entries_by_date[THU] = []
add("C3", [11700, 23400, 35100, 46800], day=THU)
add("C3", [11700, 23400, 35100, 46800], slots=SHIFT_SLOTS["2nd Shift"], day=THU)
# PRE is before anyone entered downtime (PREV, 9/1, is the first day with
# any), so C1's shift there is outside OEE: neither counted nor left out.
PRE = date(2026, 8, 25)
entries_by_date[PRE] = []
add("C1", [11700, 23400, 35100, 46800], day=PRE)

print("\n== 7 days (compute_oee_range) ==")
week = oee.compute_oee_range(SAT, 7, now=NOW_SAT)
ALL = "All"
check("window", (week["start"], week["end"], week["days"]), ("2026-09-06", "2026-09-12", 7))
check("compared with the 7 days before",
      (week["previous_start"], week["previous_end"]), ("2026-08-30", "2026-09-05"))
check("every machine is in it", len(week["machines"]), len(MACHINE_IDS))
check("all shifts plus each one",
      set(week["floor"]), {ALL, "1st Shift", "2nd Shift", "3rd Shift"})

floor = week["floor"][ALL]
# DAY: 6 on the 1st Shift, WS2/WS3's night crews, C1's 3rd. SAT: FM1, FM2,
# FM3 x3, WS6's 2nd. Left out: C4 (bad last reading), C5 and WS4 (no
# downtime), C6 (no production), C3 x2 on THU (no downtime).
check("counted machine-shifts", floor["counted"], 15)
check("left out", floor["left_out"], 6)
check("not thin", floor["thin"], False)
good = (46800 + 15750 + 46800 + 59400 + 39600 + 72000   # DAY 1st
        + 59400 + 59400 + 46800                         # DAY 2nd, 3rd
        + 24300 + 16200 + 24300 + 24300 + 16200 + 19800)  # SAT
check("good is every counted shift's", floor["current"]["good"], good)
# Everything ran at standard (0.75) except C2's 25.2% on DAY.
floor_oee = good / ((good - 15750) / 0.75 + 130 * 480)
check("a week's OEE is total good over total possible", floor["current"]["oee"], floor_oee)
per_shift = [0.75] * 14 + [15750 / (130 * 480)]
check("...which is not the average of the shifts' OEE",
      abs(floor["current"]["oee"] - sum(per_shift) / len(per_shift)) > 0.003, True)
check("the counts are named shifts, not machines",
      ("shifts" in floor["current"], "machines" in floor["current"]), (True, False))
check("C3 has no scrap on DAY, so P and Q cover 14", floor["current"]["split_shifts"], 14)
check("the three shifts add up to All",
      sum(week["floor"][s]["current"]["good"] for s in SHIFT_SLOTS), good)
check("...and so do their counts",
      sum(week["floor"][s]["counted"] + week["floor"][s]["left_out"] for s in SHIFT_SLOTS), 21)
check("the previous week: PREV's three shifts",
      (floor["previous_counted"], floor["previous"]["oee"]), (3, 0.75))
check("change is this week minus last", floor["change"], floor_oee - 0.75)

print("\n== 7 days, per machine ==")
c1 = week["machines"]["C1"]
check("C1 counted its 1st and 3rd on DAY", (c1[ALL]["counted"], c1[ALL]["current"]["good"]),
      (2, 93600))
check("C1 at standard all week", c1[ALL]["current"]["oee"], 0.75)
check("C1's A x P x Q == oee (same shifts have scrap)", c1[ALL]["current"]["oee_from_factors"], 0.75)
check("unchanged on last week", c1[ALL]["change"], 0.0)
check("C1's 1st Shift alone", (c1["1st Shift"]["counted"], c1["1st Shift"]["current"]["good"]),
      (1, 46800))
check("C1's 2nd Shift: nothing either week, no change", (c1["2nd Shift"]["current"], c1["2nd Shift"]["change"]),
      (None, None))
c2 = week["machines"]["C2"][ALL]
check("C2 fell from 75% to 25.2%", c2["change"], 15750 / (130 * 480) - 0.75)
check("C2's Top reasons", c2["reasons"],
      [{"code": "LACK_OPER", "label": "Lack of Operator", "is_planned": False,
        "minutes": 240, "shifts": 1}])

c3 = week["machines"]["C3"][ALL]
check("C3: one counted, two left out", (c3["counted"], c3["left_out"]), (1, 2))
check("so it's thin", c3["thin"], True)
check("and gets no change, though both weeks have a number",
      (c3["current"] is not None, c3["previous"] is not None, c3["change"]), (True, True, None))
check("its left-out shifts, with the date and why", c3["left_out_shifts"], [
    {"date": "2026-09-10", "shift": "1st Shift", "reasons": ["no_downtime"]},
    {"date": "2026-09-10", "shift": "2nd Shift", "reasons": ["no_downtime"]},
])
check("C3's 2nd Shift view: left out only, no number",
      (week["machines"]["C3"]["2nd Shift"]["current"], week["machines"]["C3"]["2nd Shift"]["thin"]),
      (None, True))

check("C4's bad last reading is listed", week["machines"]["C4"][ALL]["left_out_shifts"],
      [{"date": "2026-09-08", "shift": "1st Shift", "reasons": ["final_reading_low"]}])
check("C6's downtime with no production is listed", week["machines"]["C6"][ALL]["left_out_shifts"],
      [{"date": "2026-09-08", "shift": "1st Shift", "reasons": ["no_production"]}])
check("C6 has no counted reasons", week["machines"]["C6"][ALL]["reasons"], [])
check("WS4 on the Saturday", [s["date"] for s in week["machines"]["WS4"][ALL]["left_out_shifts"]],
      ["2026-09-12"])
check("P3 wasn't scheduled: neither counted nor left out",
      (week["machines"]["P3"][ALL]["counted"], week["machines"]["P3"][ALL]["left_out"]), (0, 0))
as1 = week["machines"]["AS1"][ALL]
check("a machine nobody ran has nothing, and isn't thin",
      (as1["current"], as1["counted"], as1["left_out"], as1["thin"]), (None, 0, 0, False))

print("\n== 7 days, per zone ==")
b3 = week["zones"]["b3"][ALL]
check("b3: C1 x2, C2, C3 counted", b3["counted"], 4)
check("b3: C3 x2, C4, C5, C6 left out", b3["left_out"], 5)
check("so b3 is thin this week", b3["thin"], True)
check("b3 oee is component-summed",
      b3["current"]["oee"], (93600 + 15750 + 46800) / ((93600 + 46800) / 0.75 + 130 * 480))

print("\n== 7 days, a column per day, newest first ==")
check("one column per day", week["column_days"], 1)
check("dates run from SAT back to the Sunday",
      [(c["start"], c["end"]) for c in week["columns"]],
      [(f"2026-09-{d:02d}", f"2026-09-{d:02d}") for d in range(12, 5, -1)])
cols = floor["columns"]
check("SAT (column 0): FM1, FM2, FM3 x3, WS6 counted, WS4 left out",
      (cols[0]["counted"], cols[0]["left_out"], cols[0]["oee"]), (6, 1, 0.75))
day_good = 46800 + 15750 + 46800 + 59400 + 39600 + 72000 + 59400 + 59400 + 46800
check("DAY (column 4): its nine shifts, C4/C5/C6 left out",
      (cols[4]["counted"], cols[4]["left_out"]), (9, 3))
check("DAY's OEE is its own total over its own possible",
      cols[4]["oee"], day_good / ((day_good - 15750) / 0.75 + 130 * 480))
check("THU: only C3's two left-out shifts, so no number and thin",
      (cols[2]["oee"], cols[2]["counted"], cols[2]["left_out"], cols[2]["thin"]), (None, 0, 2, True))
check("the Sunday: nothing", cols[6], {"oee": None, "counted": 0, "left_out": 0, "thin": False})
check("the columns add up to the week",
      (sum(c["counted"] for c in cols), sum(c["left_out"] for c in cols)), (15, 6))
check("per machine: C3 counted on DAY, left out on THU",
      [(c["counted"], c["left_out"]) for c in week["machines"]["C3"][ALL]["columns"]],
      [(0, 0), (0, 0), (0, 2), (0, 0), (1, 0), (0, 0), (0, 0)])
check("per shift: DAY's 2nd Shift is WS2 and WS3's night crews",
      week["floor"]["2nd Shift"]["columns"][4]["counted"], 2)
check("per zone: b3 on DAY is C1 x2, C2, C3; C4-C6 left out",
      (week["zones"]["b3"][ALL]["columns"][4]["counted"],
       week["zones"]["b3"][ALL]["columns"][4]["left_out"]), (4, 3))

print("\n== 7 days, the Pareto ==")


def pareto_totals(range_payload, key=ALL):
    totals = {}
    for machine in range_payload["machines"].values():
        for reason in machine[key]["pareto"]:
            totals[reason["code"]] = totals.get(reason["code"], 0) + reason["minutes"]
    return totals


check("reasons summed across days and shifts",
      pareto_totals(week), {"LACK_OPER": 300, "EQUIP_FAIL": 75, "SETUP": 30})
check("C6's Equipment Failure is in it despite no OEE", week["machines"]["C6"][ALL]["pareto"][0]["minutes"], 75)
check("not-scheduled FM1 afternoon's Delivery left out", "DELIVERY" not in pareto_totals(week), True)
check("the absent 3rd Shift's Roll Change left out", "ROLL_CHANGE" not in pareto_totals(week), True)
check("per shift: the 2nd Shift has FM3's Lack of Operator only",
      pareto_totals(week, "2nd Shift"), {"LACK_OPER": 60})
check("records: 11 on the Tuesday, 6 on the Saturday",
      sum(m[ALL]["downtime_records"] for m in week["machines"].values()), 17)
check("ran-clean shifts count as records", week["machines"]["FM3"][ALL]["downtime_records"], 3)
check("missing: production with no downtime",
      {m: v[ALL]["downtime_missing"] for m, v in week["machines"].items() if v[ALL]["downtime_missing"]},
      {"C3": 2, "C5": 1, "WS4": 1})

early = oee.compute_oee_range(SAT, 7, now=datetime(2026, 9, 12, 11, 0))
check("a shift still running isn't missing its downtime yet",
      {m: v[ALL]["downtime_missing"] for m, v in early["machines"].items() if v[ALL]["downtime_missing"]},
      {"C3": 2, "C5": 1})
check("...nor left out", early["machines"]["WS4"][ALL]["left_out"], 0)
check("FM1's 1st Shift counts on its elapsed part",
      early["machines"]["FM1"][ALL]["current"]["ppt_minutes"], 240)

print("\n== 30 days ==")
month = oee.compute_oee_range(SAT, 30, now=NOW_SAT)
check("30 days ends on the same day", (month["start"], month["end"]), ("2026-08-14", "2026-09-12"))
check("PREV is inside it now", month["floor"][ALL]["counted"], 18)
check("tracking began on PREV", (month["tracking_start"], month["tracked_start"]),
      ("2026-09-01", "2026-09-01"))
check("PRE's shift is before tracking: not left out", month["floor"][ALL]["left_out"], 6)
check("...and C1 has nothing left out", month["machines"]["C1"][ALL]["left_out"], 0)
check("...nor missing in the Pareto's coverage", month["machines"]["C1"][ALL]["downtime_missing"], 0)
check("weeks entirely before tracking are marked",
      [c["before_tracking"] for c in month["columns"]], [False, False, True, True, True])
check("a column per week, newest first, the leftover two days last",
      [(c["start"], c["end"]) for c in month["columns"]],
      [("2026-09-06", "2026-09-12"), ("2026-08-30", "2026-09-05"), ("2026-08-23", "2026-08-29"),
       ("2026-08-16", "2026-08-22"), ("2026-08-14", "2026-08-15")])
check("SAT's week holds the 15, PREV's the 3",
      [c["counted"] for c in month["floor"][ALL]["columns"]], [15, 3, 0, 0, 0])
check("nothing in the 30 days before, so no change",
      (month["floor"][ALL]["previous"], month["floor"][ALL]["change"]), (None, None))
check("the Pareto holds the same reasons (PREV ran clean)", pareto_totals(month), pareto_totals(week))
check("a window that misses every day is empty",
      oee.compute_oee_range(date(2026, 9, 7), 1, now=NOW_SAT)["floor"][ALL]["current"], None)

print("\n== any other length (a custom period) ==")
fortnight = oee.compute_oee_range(SAT, 15, now=NOW_SAT)
check("15 days back from SAT, compared with the 15 before",
      (fortnight["start"], fortnight["previous_start"], fortnight["previous_end"]),
      ("2026-08-29", "2026-08-14", "2026-08-28"))
check("still a column per day", (fortnight["column_days"], len(fortnight["columns"])), (1, 15))
check("holds SAT's week and PREV", fortnight["floor"][ALL]["counted"], 18)
sixteen = oee.compute_oee_range(SAT, 16, now=NOW_SAT)
check("16 days: a column per week, the leftover two days last",
      [(c["start"], c["end"]) for c in sixteen["columns"]],
      [("2026-09-06", "2026-09-12"), ("2026-08-30", "2026-09-05"), ("2026-08-28", "2026-08-29")])
check("days per column, by period length",
      [oee_column_days(d) for d in (1, 7, 15, 16, 30, 60)], [1, 1, 1, 7, 7, 7])

print("\n== comparing with a period that was only partly tracked ==")
# Ending THU: the week before is 8/28-9/3, of which 9/1-9/3 were tracked.
thu = oee.compute_oee_range(THU, 7, now=NOW_SAT)
check("3 of the previous 7 days were tracked", thu["previous_tracked_days"], 3)
check("PREV's shifts are there, but aren't compared against",
      (thu["floor"][ALL]["previous"]["shifts"], thu["floor"][ALL]["change"]), (3, None))
# Ending the day after: 8/29-9/4, so 4 of 7 were tracked.
fri = oee.compute_oee_range(THU + timedelta(days=1), 7, now=NOW_SAT)
check("4 of 7 is enough", (fri["previous_tracked_days"], fri["floor"][ALL]["change"] is not None), (4, True))
saved_tracking_start = oee.get_oee_tracking_start
oee.get_oee_tracking_start = lambda: None
check("with no downtime anywhere, nothing is tracked, so nothing is left out",
      oee.compute_oee_range(SAT, 7, now=NOW_SAT)["floor"][ALL]["left_out"], 0)
oee.get_oee_tracking_start = saved_tracking_start
json.dumps(week)

print("\n== one day, All (/oee's All on a single day is a 1-day range) ==")
one_day = oee.compute_oee_range(DAY, 1, now=NOW)
day_report = oee.compute_oee_report(DAY, now=NOW)


def oee_of(rollup):
    return rollup["oee"] if rollup else None


# Under All each figure shows its shifts beside it; those must be the numbers
# picking that shift shows.
differ = []
for shift in SHIFT_SLOTS:
    shift_data = day_report["shifts"][shift]
    for machine_id, row in shift_data["machines"].items():
        want = row["shift"]["oee"] if row["scheduled"] and row["shift"] else None
        got = oee_of(one_day["machines"][machine_id][shift]["current"])
        if (want is None) != (got is None) or (want is not None and abs(want - got) > 1e-9):
            differ.append((machine_id, shift, want, got))
    for slug, zone in shift_data["zones"].items():
        want, got = oee_of(zone), oee_of(one_day["zones"][slug][shift]["current"])
        if (want is None) != (got is None) or (want is not None and abs(want - got) > 1e-9):
            differ.append((slug, shift, want, got))
check("each shift under All matches that shift's own view, per machine and zone", differ, [])
check("the floor's 1st Shift matches its rollup",
      one_day["floor"]["1st Shift"]["current"]["oee"], day_report["shifts"]["1st Shift"]["rollup"]["oee"])
check("All is the day's column in the 7-day view",
      (one_day["floor"][ALL]["counted"], one_day["floor"][ALL]["left_out"]),
      (cols[4]["counted"], cols[4]["left_out"]))
check("...with the same OEE", one_day["floor"][ALL]["current"]["oee"], cols[4]["oee"])
check("C1's day: its 1st and 3rd Shift added up",
      (one_day["machines"]["C1"][ALL]["counted"], one_day["machines"]["C1"][ALL]["current"]["good"]),
      (2, 93600))
check("left-out shifts carry their shift, for the day's list",
      one_day["machines"]["C4"][ALL]["left_out_shifts"],
      [{"date": "2026-09-08", "shift": "1st Shift", "reasons": ["final_reading_low"]}])

print("\n== the one-day readers are the range readers, keyed without the date ==")
oee.get_downtime_for_range = lambda s, e: {
    ("C1", s, SHIFT): {"note": None, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0},
}
oee.get_schedule_for_range = lambda s, e: {("C1", s, SHIFT): False}
oee.get_shift_lengths_for_range = lambda s, e: {("C1", s, SHIFT): 4}
oee.get_shift_starts_for_range = lambda s, e: {("C1", s, "2nd Shift"): 16}
check("downtime", list(real_downtime_for_date(DAY)), [("C1", SHIFT)])
check("schedule", real_schedule_for_date(DAY), {("C1", SHIFT): False})
check("lengths", real_lengths_for_date(DAY), {("C1", SHIFT): 4})
check("starts", real_starts_for_date(DAY), {("C1", "2nd Shift"): 16})

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
