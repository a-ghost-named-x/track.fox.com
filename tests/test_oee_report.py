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
    WS3  8-hour 1st Shift, then a 12-hour crew at 6PM
    P1   10-hour 1st Shift at standard               -> 600 min, 5 slots, 0.75

And the production-day rule: C1's 3rd Shift on DAY is DAY 10PM -> DAY_AFTER
6AM, read from DAY_AFTER's night entries.

Then a short Saturday (SAT), the 6-hour pattern with typed hours:

    FM1  6 hours on the 1st Shift at standard       -> 360 min, 3 slots, 0.75
         ...its 2nd and 3rd follow the 6-hour pattern, not scheduled
    FM2  4 hours, some downtime                     -> 240 min, still 0.75 OEE
    FM3  6 / 6 / 5, all three crews ticked on       -> 12PM reset, 3rd to 12AM
    WS4  an ordinary 8h shift, downtime never entered
    WS5  a 6h morning, then an ordinary 8h 2nd Shift

and the 7/30-day Pareto over both days (compute_pareto_range).

See tests/test_oee_math.py for the unit-level arithmetic. This file checks
aggregation: percentages are never averaged, and A x P x Q must equal OEE
whenever the three factors cover the same machines.
"""
import os
import sys
import types
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_DB_PASSWORD", "stub")
try:  # pragma: no cover - environment shim
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

import app.db.entries as entries_mod  # noqa: E402
import app.db.oee as oee  # noqa: E402
from app.models import MACHINE_IDS, SHIFT_SLOTS, TIME_SLOTS  # noqa: E402

DAY = date(2026, 9, 8)
DAY_BEFORE = date(2026, 9, 7)
DAY_AFTER = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 9, 0)  # two days after, so every slot has elapsed
SAT = date(2026, 9, 12)            # a Saturday: the short-day cases
SAT_AFTER = date(2026, 9, 13)
NOW_SAT = datetime(2026, 9, 14, 9, 0)
SHIFT = "1st Shift"
S1 = SHIFT_SLOTS[SHIFT]

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
# ideal 5,400 an hour. Every short machine below is exactly at standard.
S1_SHORT = ["8AM", "10AM", "12PM"]
add("FM1", [8100, 16200, 24300], slots=S1_SHORT, day=SAT)
add("FM2", [8100, 16200, None], slots=S1_SHORT, day=SAT)          # 6AM-10AM
add("FM3", [8100, 16200, 24300], slots=S1_SHORT, day=SAT)
# The 12PM crew starts its count from zero, like any shift change.
add("FM3", [8100, 16200, 24300], slots=["2PM", "4PM", "6PM"], day=SAT)
# A 5-hour 3rd Shift, 6PM-11PM: its 12AM box is filed under the next morning.
add("FM3", [8100, 16200], slots=["8PM", "10PM"], day=SAT)
add("FM3", [20250], slots=["12AM"], day=SAT_AFTER)
add("WS4", [9900, 19800, 29700, 39600], day=SAT)                   # no downtime entered

sat_lengths = {
    ("FM1", "1st Shift"): 6,
    ("FM2", "1st Shift"): 4,
    ("FM3", "1st Shift"): 6, ("FM3", "2nd Shift"): 6, ("FM3", "3rd Shift"): 5,
    ("WS5", "1st Shift"): 6, ("WS5", "2nd Shift"): 8,
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
}
sat_scrap = {("FM1", SHIFT): 0, ("FM2", SHIFT): 0, ("FM3", SHIFT): 0,
             ("FM3", "2nd Shift"): 0, ("FM3", "3rd Shift"): 0}
# A short day's afternoon and evening shifts default to not scheduled; FM3's
# crews were ticked on.
sat_schedule = {("FM3", "2nd Shift"): True, ("FM3", "3rd Shift"): True}

lengths_by_day = {DAY: shift_lengths, SAT: sat_lengths}
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

entries_mod.get_latest_entries_for_date = lambda d: list(entries_by_date.get(d, []))
entries_mod.get_reported_slots = lambda start, end: {
    (row["machine_id"], day, row["time_slot"])
    for day, rows in entries_by_date.items() if start <= day <= end
    for row in rows
}
oee.get_latest_scrap_for_date = lambda d: dict(scrap_by_day.get(d, {}))
oee.get_latest_downtime_for_date = lambda d: dict(downtime_by_day.get(d, {}))
oee.get_schedule_for_date = lambda d: dict(schedule_by_day.get(d, {}))
oee.get_shift_lengths = lambda d: dict(lengths_by_day.get(d, {}))
oee.get_shift_lengths_for_range = lambda s, e: by_range(lengths_by_day, s, e)
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

print("\n== WS3, the manager's case: 8h 1st Shift, then a 12h crew at 6PM ==")
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
      {"shift": "2nd Shift", "hours": 12, "span": "6PM-6AM"})
check("3rd Shift completeness leaves it out (WS1, WS2, WS3, P1 have none)",
      report["shifts"]["3rd Shift"]["completeness"]["machines"], 30)

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
check("and there is no 3rd",
      report["shifts"]["3rd Shift"]["machines"]["P1"]["shift_exists"], False)

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
check("scheduled machines", c["machines"], 33)          # 34 minus unscheduled P3
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

print("\n== SHORT DAYS: the 6-hour pattern, as geometry ==")
from app.models import (  # noqa: E402
    default_scheduled, resolve_day_lengths, shift_length_conflict, shift_plan, shift_span,
    window_span,
)
check("the 6-hour plan", shift_plan(6), {
    "1st Shift": ["8AM", "10AM", "12PM"],
    "2nd Shift": ["2PM", "4PM", "6PM"],
    "3rd Shift": ["8PM", "10PM", "12AM"],
})
check("1-5 hours sit in the same windows", shift_plan(4), shift_plan(6))
check("4h on the 1st Shift is 6AM-10AM", shift_span("1st Shift", 4), "6AM-10AM")
check("...inside the 6AM-12PM window", window_span("1st Shift", 4), "6AM-12PM")
check("the 3rd Shift of a short day ends at midnight", shift_span("3rd Shift", 6), "6PM-12AM")
check("a later shift inherits the PATTERN, not the typed hours",
      resolve_day_lengths({"1st Shift": 4}),
      {"1st Shift": 4, "2nd Shift": 6, "3rd Shift": 6})
check("4 then 6 then 3 is a valid day",
      shift_length_conflict({"1st Shift": 4, "2nd Shift": 6, "3rd Shift": 3}), None)
check("6 then 8 is a valid day (12PM-2PM is idle)",
      shift_length_conflict(resolve_day_lengths({"1st Shift": 6, "2nd Shift": 8})), None)
conflict = shift_length_conflict(resolve_day_lengths({"2nd Shift": 4}))
check("a short 2nd Shift after an 8h 1st starts inside it",
      conflict is not None and "short-day shift after 1st Shift" in conflict, True)
check("a short 1st Shift defaults to scheduled", default_scheduled("1st Shift", 4), True)
check("a short day's 2nd and 3rd default to NOT scheduled",
      (default_scheduled("2nd Shift", 6), default_scheduled("3rd Shift", 6)), (False, False))

sat = oee.compute_oee_report(SAT, now=NOW_SAT)
SM = sat["shifts"][SHIFT]["machines"]

print("\n== FM1: 6 hours on the 1st Shift, at standard ==")
fm1 = SM["FM1"]
check("shift_hours", fm1["shift_hours"], 6)
check("span", fm1["span"], "6AM-12PM")
check("three checkpoints", [c["slot"] for c in fm1["checkpoints"]], S1_SHORT)
check("ppt is 360", fm1["shift"]["ppt_minutes"], 360)
check("standard scaled to 6 hours (32,400 x 6/8)", fm1["shift"]["standard"], 24300)
check("good is the 12PM reading", fm1["shift"]["good"], 24300)
check("oee is exactly 0.75 — judged on 6 hours, not 8", fm1["shift"]["oee"], 0.75)
check("A x P x Q == oee", fm1["shift"]["oee_from_factors"], 0.75)
fm1_2nd = sat["shifts"]["2nd Shift"]["machines"]["FM1"]
check("its 2nd Shift is the 6-hour 12PM-6PM", (fm1_2nd["shift_hours"], fm1_2nd["span"]),
      (6, "12PM-6PM"))
check("...and not scheduled unless ticked", fm1_2nd["scheduled"], False)
fm1_3rd = sat["shifts"]["3rd Shift"]["machines"]["FM1"]
check("its 3rd Shift is 6PM-12AM, not scheduled",
      (fm1_3rd["shift_exists"], fm1_3rd["span"], fm1_3rd["scheduled"]), (True, "6PM-12AM", False))

print("\n== FM2: 4 hours with 30 minutes of setup ==")
fm2 = SM["FM2"]
check("span is the scheduled part", fm2["span"], "6AM-10AM")
check("window is the whole shift", fm2["window_span"], "6AM-12PM")
check("still three checkpoints (the window's)", len(fm2["checkpoints"]), 3)
check("ppt is 240, not 360", fm2["shift"]["ppt_minutes"], 240)
check("shift_minutes is 240", fm2["shift_minutes"], 240)
check("availability is 210 of 240", fm2["shift"]["availability"], 210 / 240)
check("standard scaled to 4 hours", fm2["shift"]["standard"], 16200)
check("good is the 10AM reading, the 12PM box being blank", fm2["shift"]["good"], 16200)
check("oee", fm2["shift"]["oee"], 0.75)
check("no warnings", fm2["warnings"], [])

print("\n== FM3: a short day with all three crews ==")
fm3_2nd = sat["shifts"]["2nd Shift"]["machines"]["FM3"]
check("2nd Shift checkpoints", [c["slot"] for c in fm3_2nd["checkpoints"]], ["2PM", "4PM", "6PM"])
check("counted from zero at 12PM", fm3_2nd["shift"]["good"], 24300)
check("2nd Shift oee", fm3_2nd["shift"]["oee"], 0.75)
fm3_3rd = sat["shifts"]["3rd Shift"]["machines"]["FM3"]
check("5-hour 3rd Shift is 6PM-11PM", fm3_3rd["span"], "6PM-11PM")
check("its checkpoints run to midnight, the last filed next morning",
      [(c["slot"], c["date"]) for c in fm3_3rd["checkpoints"]],
      [("8PM", "2026-09-12"), ("10PM", "2026-09-12"), ("12AM", "2026-09-13")])
check("good is the 12AM reading from the next date", fm3_3rd["shift"]["good"], 20250)
check("ppt is 300", fm3_3rd["shift"]["ppt_minutes"], 300)
check("standard scaled to 5 hours stays whole", fm3_3rd["shift"]["standard"], 20250)
check("oee", fm3_3rd["shift"]["oee"], 0.75)
check("3rd Shift rollup counts FM3 only",
      sat["shifts"]["3rd Shift"]["rollup"]["machines"], 1)

print("\n== WS5: a 6-hour morning, then an ordinary 2nd Shift ==")
ws5_2nd = sat["shifts"]["2nd Shift"]["machines"]["WS5"]
check("the 2nd Shift is 2PM-10PM", ws5_2nd["span"], "2PM-10PM")
check("and scheduled by default, as any 8h shift",  ws5_2nd["scheduled"], True)
check("the 3rd follows it: 10PM-6AM",
      sat["shifts"]["3rd Shift"]["machines"]["WS5"]["span"], "10PM-6AM")

print("\n== the short day's rollup and completeness ==")
sat_rollup = sat["shifts"][SHIFT]["rollup"]
check("1st Shift rollup: FM1, FM2, FM3", sat_rollup["machines"], 3)
check("with 360 + 240 + 360 minutes of PPT", sat_rollup["ppt_minutes"], 960)
check("other-length machines on 1st Shift (FM1, FM2, FM3, WS5)",
      sat["shifts"][SHIFT]["completeness"]["other_length_machines"], 4)

print("\n== a short shift in progress ==")
mid_sat = oee.compute_oee_report(SAT, now=datetime(2026, 9, 12, 9, 0))
check("at 9AM FM2 has 120 minutes so far",
      mid_sat["shifts"][SHIFT]["machines"]["FM2"]["shift"]["ppt_minutes"], 120)
after_sat = oee.compute_oee_report(SAT, now=datetime(2026, 9, 12, 13, 0))
check("at 1PM it is capped at its 4 hours, though three slots have closed",
      after_sat["shifts"][SHIFT]["machines"]["FM2"]["shift"]["ppt_minutes"], 240)

print("\n== the 7-day Pareto (compute_pareto_range) ==")
week = oee.compute_pareto_range(SAT, 7, now=NOW_SAT)
check("window", (week["start"], week["end"], week["days"]), ("2026-09-06", "2026-09-12", 7))
check("every machine is in it", len(week["machines"]), len(MACHINE_IDS))


def floor_totals(range_payload):
    totals = {}
    for machine in range_payload["machines"].values():
        for reason in machine["reasons"]:
            totals[reason["code"]] = totals.get(reason["code"], 0) + reason["minutes"]
    return totals


check("reasons summed across days and shifts",
      floor_totals(week), {"LACK_OPER": 300, "EQUIP_FAIL": 75, "SETUP": 30})
check("not-scheduled FM1 afternoon's Delivery left out", "DELIVERY" not in floor_totals(week), True)
check("the absent 3rd Shift's Roll Change left out", "ROLL_CHANGE" not in floor_totals(week), True)
check("C2's reasons", week["machines"]["C2"]["reasons"],
      [{"code": "LACK_OPER", "label": "Lack of Operator", "is_planned": False,
        "minutes": 240, "shifts": 1}])
check("FM3's Lack of Operator came from its 2nd Shift",
      [(r["code"], r["minutes"]) for r in week["machines"]["FM3"]["reasons"]], [("LACK_OPER", 60)])
check("records: 11 on the Tuesday, 5 on the Saturday",
      sum(m["records"] for m in week["machines"].values()), 16)
check("ran-clean shifts count as records", week["machines"]["FM3"]["records"], 3)
check("missing: C5 and WS4 reported production with no downtime",
      {m: v["missing"] for m, v in week["machines"].items() if v["missing"]}, {"C5": 1, "WS4": 1})
check("a machine nobody ran isn't missing", week["machines"]["AS1"]["missing"], 0)

early = oee.compute_pareto_range(SAT, 7, now=datetime(2026, 9, 12, 11, 0))
check("a shift still running isn't missing its downtime yet",
      {m: v["missing"] for m, v in early["machines"].items() if v["missing"]}, {"C5": 1})

month = oee.compute_pareto_range(SAT, 30, now=NOW_SAT)
check("30 days ends on the same day", (month["start"], month["end"]), ("2026-08-14", "2026-09-12"))
check("and holds the same data here", floor_totals(month), floor_totals(week))
check("a window that misses both days is empty",
      floor_totals(oee.compute_pareto_range(date(2026, 9, 7), 1, now=NOW_SAT)), {})
json.dumps(week)

print("\n== the one-day readers are the range readers, keyed without the date ==")
oee.get_downtime_for_range = lambda s, e: {
    ("C1", s, SHIFT): {"note": None, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0},
}
oee.get_schedule_for_range = lambda s, e: {("C1", s, SHIFT): False}
oee.get_shift_lengths_for_range = lambda s, e: {("C1", s, SHIFT): 4}
check("downtime", list(real_downtime_for_date(DAY)), [("C1", SHIFT)])
check("schedule", real_schedule_for_date(DAY), {("C1", SHIFT): False})
check("lengths", real_lengths_for_date(DAY), {("C1", SHIFT): 4})

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
