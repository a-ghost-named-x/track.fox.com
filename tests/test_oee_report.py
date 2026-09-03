"""Integration check for compute_oee_report(), with the DB reads stubbed out.

Run it directly — there's no test framework in this project:

    python tests/test_oee_report.py

Exercises the whole report builder against a synthetic day built around the
cases that actually bite:

    C1   hits standard exactly, ran clean, zero scrap  -> must score 0.75
    C2   missing its 10AM checkpoint                   -> two slots uncountable
    C3   a checkpoint typed lower than the one before  -> flagged, excluded
    P1   ran, but marked not scheduled                 -> absent from rollups
    AS1  real losses incl. planned maintenance         -> feeds the Pareto
    AS6  units and scrap but NO downtime entry         -> OEE N/A, not 100%

See tests/test_oee_math.py for the unit-level arithmetic. The rule this file
guards is the aggregation: percentages are never averaged, and A x P x Q must
reconstruct OEE whenever the three factors describe the same machines.
"""
import os
import sys
import types
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("APP_DB_PASSWORD", "stub")
try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

import app.db.entries as entries_mod
import app.db.oee as oee
from app.models import SHIFT_SLOTS

DAY = date(2026, 9, 2)
NOW = datetime(2026, 9, 3, 9, 0)  # the day after, so all slots are elapsed
S1 = SHIFT_SLOTS["1st Shift"]  # 8AM 10AM 12PM 2PM

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

# --- synthetic day ---------------------------------------------------------
entries = []
def add_units(machine, cumulative_by_slot, operator="Sam"):
    for slot, value in cumulative_by_slot.items():
        entries.append({
            "machine_id": machine, "operator": operator, "time_slot": slot,
            "units_produced": value, "status": ":)", "issue": None,
            "entered_by": "1234", "entry_date": DAY, "created_at": NOW,
        })

# C1 — textbook: hits standard exactly, clean, no scrap. Must score 0.75.
add_units("C1", dict(zip(S1, [11700, 23400, 35100, 46800])))
# C2 — missing the 10AM checkpoint entirely.
add_units("C2", {"8AM": 11700, "12PM": 35100, "2PM": 46800})
# C3 — 12PM checkpoint typed lower than 10AM: impossible on a cumulative counter.
add_units("C3", dict(zip(S1, [11700, 23400, 20000, 46800])))
# P1 — ran, but marked not scheduled; must be excluded from the rollup.
add_units("P1", dict(zip(S1, [14400, 28800, 43200, 57600])))
# AS1 — real losses, two reasons in one slot, for the Pareto.
add_units("AS1", dict(zip(S1, [2000, 4000, 6000, 8000])))
# AS6 — units and scrap but NO downtime entry: OEE must be N/A, not 100%.
add_units("AS6", dict(zip(S1, [2250, 4500, 6750, 9000])))

scrap = {}
for slot, value in zip(S1, [0, 0, 0, 0]):
    scrap[("C1", slot)] = value
for slot, value in zip(S1, [50, 100, 150, 200]):
    scrap[("AS1", slot)] = value
for slot, value in zip(S1, [10, 20, 30, 40]):
    scrap[("AS6", slot)] = value

def clean(slot_list, machine):
    return {(machine, s): {"note": None, "reasons": [], "planned_minutes": 0,
                           "unplanned_minutes": 0} for s in slot_list}

downtime = {}
downtime.update(clean(S1, "C1"))
downtime.update(clean(S1, "C2"))
downtime.update(clean(S1, "C3"))
downtime.update(clean(S1, "P1"))
# AS1: 8AM has two unplanned causes; 10AM has planned maintenance.
downtime[("AS1", "8AM")] = {
    "note": "belt", "planned_minutes": 0, "unplanned_minutes": 25,
    "reasons": [
        {"code": "MATL", "label": "Material wait or shortage", "minutes": 15, "is_planned": False},
        {"code": "MECH", "label": "Mechanical failure", "minutes": 10, "is_planned": False},
    ],
}
downtime[("AS1", "10AM")] = {
    "note": None, "planned_minutes": 30, "unplanned_minutes": 0,
    "reasons": [{"code": "PM", "label": "Planned maintenance", "minutes": 30, "is_planned": True}],
}
downtime[("AS1", "12PM")] = {"note": None, "reasons": [], "planned_minutes": 0, "unplanned_minutes": 0}
downtime[("AS1", "2PM")] = {
    "note": None, "planned_minutes": 0, "unplanned_minutes": 40,
    "reasons": [{"code": "MATL", "label": "Material wait or shortage", "minutes": 40, "is_planned": False}],
}

standards = {}
for machine, inc in INCREMENTS.items():
    for label, slots in SHIFT_SLOTS.items():
        for position, slot in enumerate(slots, start=1):
            standards[(machine, slot)] = inc * position

ideal = {m: inc / 1.5 for m, inc in INCREMENTS.items()}

# --- stub the reads --------------------------------------------------------
entries_mod.get_latest_entries_for_date = lambda d: entries if d == DAY else []
oee.get_latest_scrap_for_date = lambda d: scrap if d == DAY else {}
oee.get_latest_downtime_for_date = lambda d: downtime if d == DAY else {}
oee.get_schedule_for_date = lambda d: {("P1", "1st Shift"): False}
oee.get_ideal_rates = lambda: ideal
oee.get_slot_standards = lambda: standards

report = oee.compute_oee_report(DAY, now=NOW)

failures = []
def check(label, got, want, tol=1e-9):
    ok = (got is None and want is None) or (
        isinstance(got, (int, float)) and isinstance(want, (int, float))
        and abs(got - want) <= tol
    ) or got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)

shift = report["shifts"]["1st Shift"]
M = shift["machines"]

print("\n== C1: at standard, clean, no scrap ==")
check("oee", M["C1"]["shift"]["oee"], 0.75)
check("availability", M["C1"]["shift"]["availability"], 1.0)
check("quality", M["C1"]["shift"]["quality"], 1.0)
check("performance", M["C1"]["shift"]["performance"], 0.75)
check("A x P x Q == oee", M["C1"]["shift"]["oee_from_factors"], 0.75)
check("pct_of_standard", M["C1"]["shift"]["pct_of_standard"], 1.0)
check("slots counted", M["C1"]["slots_counted"], 4)
check("good", M["C1"]["shift"]["good"], 46800)

print("\n== C2: missing 10AM -> only contiguous slots count ==")
check("slots counted (8AM + 2PM only)", M["C2"]["slots_counted"], 2)
check("slots elapsed", M["C2"]["slots_elapsed"], 4)
check("10AM has no units", M["C2"]["slots"]["10AM"]["has_units"], False)
check("12PM delta uncomputable", M["C2"]["slots"]["12PM"]["good"], None)
check("2PM delta fine", M["C2"]["slots"]["2PM"]["good"], 11700)
check("oee covers counted slots only",
      M["C2"]["shift"]["oee"], (11700 + 11700) / (7800 / 60 * 240))

print("\n== C3: backwards cumulative counter is flagged and excluded ==")
check("12PM flagged", M["C3"]["slots"]["12PM"]["flags"], ["negative_units"])
check("12PM not counted", M["C3"]["slots"]["12PM"]["counted"], False)
# Both flags are correct: 46800 - 20000 = 26800, which is also over the
# 15600 two-hour ceiling. One bad checkpoint poisons two deltas.
check("both flags surfaced", M["C3"]["flags"], ["negative_units", "over_ceiling"])
check("2PM over ceiling also caught",
      "over_ceiling" in M["C3"]["slots"]["2PM"]["flags"], True)
check("only clean slots counted", M["C3"]["slots_counted"], 2)

print("\n== P1: not scheduled ==")
check("scheduled false", M["P1"]["scheduled"], False)
check("still computed per-slot", M["P1"]["slots"]["8AM"]["oee"] is not None, True)

print("\n== AS6: units + scrap but no downtime entry ==")
check("oee is None, not 100%", M["AS6"]["shift"], None)
check("8AM has_downtime false", M["AS6"]["slots"]["8AM"]["has_downtime"], False)
check("8AM availability None", M["AS6"]["slots"]["8AM"]["availability"], None)
check("nothing counted", M["AS6"]["slots_counted"], 0)

print("\n== AS1: real losses, planned downtime out of the denominator ==")
as1 = M["AS1"]["shift"]
ideal_min = 1680 / 60  # 28 units/min
check("ppt = 480 - 30 planned", as1["ppt_minutes"], 450)
check("run = ppt - 65 unplanned", as1["run_minutes"], 385)
check("planned minutes", as1["planned_minutes"], 30)
check("unplanned minutes", as1["unplanned_minutes"], 65)
check("good", as1["good"], 8000)
check("scrap", as1["scrap"], 200)
check("oee", as1["oee"], 8000 / (ideal_min * 450))
check("availability", as1["availability"], 385 / 450)
check("quality", as1["quality"], 8000 / 8200)
check("A x P x Q == oee", as1["oee_from_factors"], as1["oee"])

print("\n== rollup excludes the unscheduled machine ==")
rollup = shift["rollup"]
expected_machines = 4  # C1, C2, C3, AS1 (P1 unscheduled, AS6 nothing counted)
check("machines counted", rollup["machines"], expected_machines)
check("P1 good not in rollup", rollup["good"] < 46800 + 57600, True)
check("rollup availability differs from uptime (mixed rates)",
      abs(rollup["availability"] - rollup["uptime"]) > 1e-6, True)

# C2 and C3 have no scrap, so the scrap-complete subset (C1, AS1) is NARROWER
# than the OEE population (C1, C2, C3, AS1). A x P x Q would then mix two
# populations, so it must be withheld rather than reported as a number that
# fails its own identity.
check("subset is narrower", rollup["split_machines"], 2)
check("identity withheld on mixed populations", rollup["oee_from_factors"], None)
check("subset still reports its own consistent oee",
      rollup["split_oee"] is not None, True)

print("== same day, but with scrap filled in for every machine ==")
# Once the populations match, A x P x Q must reconstruct the rollup OEE exactly.
for _m in ("C2", "C3"):
    for _slot, _value in zip(S1, [25, 50, 75, 100]):
        scrap[(_m, _slot)] = _value

report2 = oee.compute_oee_report(DAY, now=NOW)
rollup2 = report2["shifts"]["1st Shift"]["rollup"]
check("populations now match", rollup2["split_machines"], rollup2["machines"])
check("rollup A x P x Q == oee", rollup2["oee_from_factors"], rollup2["oee"])
check("rollup oee unchanged by scrap (scrap cancels out)",
      rollup2["oee"], rollup["oee"])

print("\n== pareto ==")
pareto = shift["pareto"]
codes = [p["code"] for p in pareto]
check("ranked by minutes", codes, ["MATL", "PM", "MECH"])
check("MATL total minutes", pareto[0]["minutes"], 55)
check("MATL occurrences", pareto[0]["occurrences"], 2)
check("MATL share of unplanned", pareto[0]["pct_of_unplanned"], 55 / 65)
check("PM is planned", pareto[1]["is_planned"], True)
check("PM has no unplanned share", pareto[1]["pct_of_unplanned"], None)

print("\n== completeness is per column ==")
c = shift["completeness"]
check("slots expected", c["slots_expected"], 4)
check("scheduled machines", c["machines"], 33)  # 34 minus unscheduled P1
check("units complete for 4 machines", c["units"], 4)  # C1 C3 AS1 AS6 (C2 has a gap)
check("scrap complete for 3", c["scrap"], 3)  # C1 AS1 AS6
check("downtime complete for 4", c["downtime"], 4)  # C1 C2 C3 AS1

print("\n== per-zone rollups come from the server, not the browser ==")
zones = shift["zones"]
# b3 (Combo/FMW) holds C1-C11 and C14-C16, so its rollup covers exactly the
# three C machines with countable data in this fixture.
b3 = zones["b3"]
check("b3 machine count", b3["machines"], 3)
check("b3 oee is component-summed, not an average of the three",
      b3["oee"],
      (46800 + 23400 + 23400) / (7800 / 60 * (480 + 240 + 240)))
# b4 is Poly, and its only machine with data is the unscheduled P1 — so the
# zone has no rollup at all rather than a zero.
check("b4 rollup is None (only machine is unscheduled)", zones["b4"], None)
# ws has no data whatsoever in this fixture.
check("ws rollup is None (no data)", zones["ws"], None)
check("leno rollup exists (AS1)", zones["leno"]["machines"], 1)
check("leno oee matches AS1's own", zones["leno"]["oee"], M["AS1"]["shift"]["oee"])

print("\n== payload is JSON-serialisable (the API returns it directly) ==")
import json
try:
    json.dumps(report)
    print("  PASS  json.dumps succeeded")
except TypeError as exc:
    print(f"  FAIL  json.dumps: {exc}")
    failures.append("json serialisable")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
