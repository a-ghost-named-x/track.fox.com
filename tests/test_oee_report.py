"""Integration check for compute_oee_report(), with the DB reads stubbed out.

Run it from anywhere:

    python tests/test_oee_report.py

Exercises the whole report builder at SHIFT grain against the cases that
actually bite:

    C1   hits standard, ran clean, zero scrap        -> must score 0.75
    C2   real row: blank tail + 240 min no operator  -> 25.2%, availability 50%
    C3   mid-shift typo the counter recovers from    -> warns, still counts
    C4   LAST reading below an earlier one           -> hard flag, excluded
    C5   production but no downtime record           -> OEE N/A, not 100%
    C6   downtime record but no production           -> no OEE, still in Pareto
    P3   ran, but marked not scheduled               -> absent from rollups

Plus the long-shift cases (shift length is per machine per production day):

    WS1  12-hour 1st Shift at standard               -> 720 min, 6 slots, 0.75
         ...and its 2nd Shift defaults to NOT scheduled
    WS2  12-hour day with a 6PM-6AM crew ticked on   -> stitched across two dates
    WS3  ran 12 hours YESTERDAY                      -> no 3rd Shift row today
    P1   10-hour 1st Shift at standard               -> 600 min, 5 slots, 0.75

See tests/test_oee_math.py for the unit-level arithmetic. The rule this file
guards is aggregation: percentages are never averaged, and A x P x Q must
reconstruct OEE whenever the three factors describe the same machines.
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

entries_by_date = {DAY: [], DAY_AFTER: []}


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

# Long shifts. The crew keeps the count running past 2PM, exactly as the floor
# does it: the 4PM and 6PM boxes hold the cumulative-since-6AM number.
add("WS1", [9900, 19800, 29700, 39600, 49500, 59400], slots=TIME_SLOTS[:6])
add("P1", [14400, 28800, 43200, 57600, 72000], slots=TIME_SLOTS[:5])
# WS2's night crew: 8PM and 10PM are filed under DAY, 12AM-6AM under DAY_AFTER,
# which is how the 2-hour rounds already enter an overnight shift.
add("WS2", [9900, 19800], slots=["8PM", "10PM"])
add("WS2", [29700, 39600, 49500, 59400], slots=["12AM", "2AM", "4AM", "6AM"], day=DAY_AFTER)

# Per (machine, production day). WS3 ran 12 hours YESTERDAY, so the 3rd Shift
# row filed under DAY (the 10PM-6AM crew) is one it never had.
shift_lengths = {
    ("WS1", DAY): 12,
    ("WS2", DAY): 12,
    ("P1", DAY): 10,
    ("WS3", DAY_BEFORE): 12,
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
}
scrap = {("C1", SHIFT): 0, ("C2", SHIFT): 250, ("C4", SHIFT): 100,
         ("WS1", SHIFT): 0, ("P1", SHIFT): 0, ("WS2", "2nd Shift"): 0}

entries_mod.get_latest_entries_for_date = lambda d: list(entries_by_date.get(d, []))
oee.get_latest_scrap_for_date = lambda d: dict(scrap) if d == DAY else {}
oee.get_latest_downtime_for_date = lambda d: dict(downtime) if d == DAY else {}
# WS2's night crew is the exception that has to be ticked on: a 12-hour day's
# 2nd Shift defaults to not scheduled.
oee.get_schedule_for_date = lambda d: {("P3", SHIFT): False, ("WS2", "2nd Shift"): True}
oee.get_shift_lengths = lambda dates: {
    key: hours for key, hours in shift_lengths.items() if key[1] in dates
}
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
      report["shifts"]["2nd Shift"]["rollup"]["machines"], 1)

print("\n== WS3 ran 12 hours yesterday: no 3rd Shift row today ==")
ws3_3rd = report["shifts"]["3rd Shift"]["machines"]["WS3"]
check("shift does not exist", ws3_3rd["shift_exists"], False)
check("so it is not scheduled", ws3_3rd["scheduled"], False)
check("and carries yesterday's length for the message", ws3_3rd["shift_hours"], 12)
check("its production day is yesterday", ws3_3rd["production_day"], "2026-09-07")
check("3rd Shift completeness leaves it out",
      report["shifts"]["3rd Shift"]["completeness"]["machines"], 33)
check("WS3's 1st Shift TODAY is an ordinary 8-hour one", M["WS3"]["shift_hours"], 8)
check("...that exists", M["WS3"]["shift_exists"], True)

print("\n== P1: 10-hour 1st Shift at standard ==")
p1 = M["P1"]
check("five checkpoints", len(p1["checkpoints"]), 5)
check("span", p1["span"], "6AM-4PM")
check("ppt is 600", p1["shift"]["ppt_minutes"], 600)
check("standard is 57,600 x 1.25", p1["shift"]["standard"], 72000)
check("oee is exactly 0.75", p1["shift"]["oee"], 0.75)
check("its 2nd Shift would be 4PM-2AM",
      report["shifts"]["2nd Shift"]["machines"]["P1"]["span"], "4PM-2AM")
check("and there is no 3rd",
      report["shifts"]["3rd Shift"]["machines"]["P1"]["shift_exists"], True)  # P1 was 8h YESTERDAY

print("\n== ordinary machines are untouched by all of this ==")
check("C1 still has four checkpoints", len(M["C1"]["checkpoints"]), 4)
check("C1 shift_hours", M["C1"]["shift_hours"], 8)
check("C1 span", M["C1"]["span"], "6AM-2PM")
check("C1 3rd Shift exists and is scheduled",
      report["shifts"]["3rd Shift"]["machines"]["C1"]["scheduled"], True)

print("\n== rollup ==")
rollup = shift["rollup"]
# C1, C2, C3, WS1, P1 countable and scheduled. C4 flagged, C5/C6 incomplete,
# P3 excluded.
check("machines counted", rollup["machines"], 5)
check("P3's 64,800 is not in the rollup",
      rollup["good"], 46800 + 15750 + 46800 + 59400 + 72000)
check("scrap subset is narrower (C3 has none)", rollup["split_machines"], 4)
check("identity withheld on mixed populations", rollup["oee_from_factors"], None)
check("a 12-hour machine contributes 720 minutes of PPT, not 480",
      rollup["ppt_minutes"], 480 * 3 + 720 + 600)

print("\n== per-zone rollups come from the server ==")
zones = shift["zones"]
check("b3 holds the C machines", zones["b3"]["machines"], 3)
check("b3 oee is component-summed, not averaged",
      zones["b3"]["oee"], (46800 + 15750 + 46800) / (130 * 480 * 3))
check("b4 holds P1 only — P3 is unscheduled", zones["b4"]["machines"], 1)
check("ws holds WS1 (WS2 only ran nights)", zones["ws"]["machines"], 1)

print("\n== pareto ==")
pareto = shift["pareto"]
check("ranked by minutes", [p["code"] for p in pareto], ["LACK_OPER", "EQUIP_FAIL"])
check("LACK_OPER minutes", pareto[0]["minutes"], 240)
check("C6 contributes despite having no OEE", pareto[1]["minutes"], 75)
check("share of unplanned", pareto[0]["pct_of_unplanned"], 240 / 315)

print("\n== completeness is per column, counted in machines ==")
c = shift["completeness"]
check("scheduled machines", c["machines"], 33)          # 34 minus unscheduled P3
check("production entered", c["production"], 7)         # C1-C5, WS1, P1 (P3 excluded)
check("downtime entered", c["downtime"], 7)             # C1-C4, C6, WS1, P1
check("scrap entered", c["scrap"], 5)                   # C1, C2, C4, WS1, P1
check("long-shift machines", c["long_shift_machines"], 3)  # WS1, WS2, P1
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

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
