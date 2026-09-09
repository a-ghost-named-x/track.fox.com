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
from app.models import MACHINE_IDS, SHIFT_SLOTS  # noqa: E402

DAY = date(2026, 9, 8)
NOW = datetime(2026, 9, 9, 9, 0)  # the day after, so every slot has elapsed
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

entries = []


def add(machine, values):
    for slot, value in zip(S1, values):
        if value is None:
            continue  # a blank checkpoint is an absent row, not a null one
        entries.append({
            "machine_id": machine, "operator": "Okafor", "time_slot": slot,
            "units_produced": value, "status": ":)", "issue": None,
            "entered_by": "4471", "entry_date": DAY, "created_at": NOW,
        })


add("C1", [11700, 23400, 35100, 46800])       # textbook
add("C2", [10500, 15750, 15750, None])        # the real C2 row
add("C3", [11700, 23400, 20000, 46800])       # typo that recovers
add("C4", [11700, 23400, 35100, 20000])       # final reading low
add("C5", [11700, 23400, 35100, 46800])       # no downtime record below
add("P3", [16200, 32400, 48600, 64800])       # unscheduled below

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
}
scrap = {("C1", SHIFT): 0, ("C2", SHIFT): 250, ("C4", SHIFT): 100}

entries_mod.get_latest_entries_for_date = lambda d: entries if d == DAY else []
oee.get_latest_scrap_for_date = lambda d: dict(scrap) if d == DAY else {}
oee.get_latest_downtime_for_date = lambda d: dict(downtime) if d == DAY else {}
oee.get_schedule_for_date = lambda d: {("P3", SHIFT): False}
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

print("\n== rollup ==")
rollup = shift["rollup"]
# C1, C2, C3 countable and scheduled. C4 flagged, C5/C6 incomplete, P3 excluded.
check("machines counted", rollup["machines"], 3)
check("P3's 64,800 is not in the rollup", rollup["good"], 46800 + 15750 + 46800)
check("scrap subset is narrower (C3 has none)", rollup["split_machines"], 2)
check("identity withheld on mixed populations", rollup["oee_from_factors"], None)

print("\n== per-zone rollups come from the server ==")
zones = shift["zones"]
check("b3 holds the C machines", zones["b3"]["machines"], 3)
check("b3 oee is component-summed, not averaged",
      zones["b3"]["oee"], (46800 + 15750 + 46800) / (130 * 480 * 3))
check("b4 is None — its only machine is unscheduled", zones["b4"], None)
check("ws is None — no data at all", zones["ws"], None)

print("\n== pareto ==")
pareto = shift["pareto"]
check("ranked by minutes", [p["code"] for p in pareto], ["LACK_OPER", "EQUIP_FAIL"])
check("LACK_OPER minutes", pareto[0]["minutes"], 240)
check("C6 contributes despite having no OEE", pareto[1]["minutes"], 75)
check("share of unplanned", pareto[0]["pct_of_unplanned"], 240 / 315)

print("\n== completeness is per column, counted in machines ==")
c = shift["completeness"]
check("scheduled machines", c["machines"], 33)          # 34 minus unscheduled P3
check("production entered", c["production"], 5)         # C1-C5 (P3 excluded)
check("downtime entered", c["downtime"], 5)             # C1-C4 + C6 (P3 excluded)
check("scrap entered", c["scrap"], 3)                   # C1, C2, C4

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

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
