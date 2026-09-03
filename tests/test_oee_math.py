"""Verifies the pure OEE arithmetic in app/db/oee.py.

Run it directly — there's no test framework in this project:

    python tests/test_oee_math.py

Exits non-zero with a list of failures if anything breaks, so it works as a
pre-push check without pytest.

The single most important assertion in here is that A x P x Q equals OEE
computed the short way (good / (ideal rate x PPT)) at BOTH the machine and
the rollup level. That identity is what caught the capacity-vs-clock-time
availability bug; if it ever fails again, the aggregation is wrong.

Nothing here touches a database. Only the pure functions are exercised — but
importing them pulls in app.db.postgres, which imports psycopg at module
scope and needs a password from config, hence the two shims below. The
psycopg shim only fires if the real driver is absent (it's present in the
container, absent on a bare dev box), so this can't shadow the genuine module.
"""
import os
import sys
import types
from pathlib import Path

# The repo root, derived from this file's own location rather than hardcoded,
# so the test runs from any working directory and on any machine.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("APP_DB_PASSWORD", "stub-for-tests")
try:  # pragma: no cover - environment shim
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    sys.modules["psycopg"] = types.ModuleType("psycopg")

from app.db.oee import (  # noqa: E402
    _accumulate,
    _accumulator_args,
    _aggregate,
    _compute_slot,
    _cumulative_deltas,
    _new_accumulator,
    _rank_pareto,
    _safe_divide,
)
from app.models import SHIFT_SLOTS, SLOT_MINUTES, elapsed_slots  # noqa: E402

failures = []


def check(label, got, want, tol=1e-9):
    ok = (got is None and want is None) or (
        got is not None and want is not None and abs(got - want) <= tol
    )
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def check_eq(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        failures.append(label)


def dt(planned=0, unplanned=0, reasons=None):
    return {
        "note": None,
        "reasons": reasons or [],
        "planned_minutes": planned,
        "unplanned_minutes": unplanned,
    }


IDEAL_C1 = 130.0  # units/minute = 7800/hr

print("\n== _safe_divide never defaults ==")
check("zero denominator", _safe_divide(5, 0), None)
check("None numerator", _safe_divide(None, 5), None)
check("None denominator", _safe_divide(5, None), None)
check("normal", _safe_divide(3, 4), 0.75)

print("\n== _cumulative_deltas ==")
slots = SHIFT_SLOTS["1st Shift"]
check_eq(
    "clean ramp",
    _cumulative_deltas(dict(zip(slots, [11700, 23400, 35100, 46800])), slots),
    {"8AM": 11700, "10AM": 11700, "12PM": 11700, "2PM": 11700},
)
check_eq(
    "gap at slot 2 poisons slots 2 and 3 only",
    _cumulative_deltas({"8AM": 11700, "10AM": None, "12PM": 35100, "2PM": 46800}, slots),
    {"8AM": 11700, "10AM": None, "12PM": None, "2PM": 11700},
)
check_eq(
    "missing first slot",
    _cumulative_deltas({"8AM": None, "10AM": 23400, "12PM": 35100, "2PM": 46800}, slots),
    {"8AM": None, "10AM": None, "12PM": 11700, "2PM": 11700},
)
check_eq(
    "nothing entered",
    _cumulative_deltas({}, slots),
    {"8AM": None, "10AM": None, "12PM": None, "2PM": None},
)

print("\n== worked example: C1, good 9000, scrap 200, 20 min unplanned ==")
s = _compute_slot(
    good=9000, scrap=200, standard=11700,
    downtime=dt(unplanned=20), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check("ppt", s["ppt_minutes"], 120)
check("run", s["run_minutes"], 100)
check("total", s["total"], 9200)
check("availability", s["availability"], 100 / 120)
check("performance", s["performance"], 9200 / 13000)
check("quality", s["quality"], 9000 / 9200)
check("oee", s["oee"], 9000 / 15600)
check("A x P x Q identity", s["availability"] * s["performance"] * s["quality"], s["oee"])
check("pct_of_standard", s["pct_of_standard"], 9000 / 11700)
check_eq("counted", s["counted"], True)
check_eq("no flags", s["flags"], [])

print("\n== the 75% anchor: at standard, no losses ==")
s = _compute_slot(
    good=11700, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check("oee at standard is exactly 0.75", s["oee"], 0.75)
check("availability", s["availability"], 1.0)
check("quality", s["quality"], 1.0)
check("performance", s["performance"], 0.75)
check("pct_of_standard", s["pct_of_standard"], 1.0)

print("\n== headroom: beating standard reaches 80/90/100% ==")
for good, want in ((12480, 0.80), (13260, 0.85), (14040, 0.90), (15600, 1.00)):
    s = _compute_slot(
        good=good, scrap=0, standard=11700,
        downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
    )
    check(f"good {good} -> oee", s["oee"], want)

print("\n== planned downtime comes OUT of the denominator ==")
s = _compute_slot(
    good=6000, scrap=0, standard=11700,
    downtime=dt(planned=30, unplanned=20), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check("ppt excludes planned", s["ppt_minutes"], 90)
check("run excludes both", s["run_minutes"], 70)
check("availability is run/ppt not run/120", s["availability"], 70 / 90)
check("oee scaled to ppt", s["oee"], 6000 / (130 * 90))
check("A x P x Q identity holds", s["availability"] * s["performance"] * s["quality"], s["oee"])

print("\n== a fully planned-down slot is unknowable, not 0% or 100% ==")
s = _compute_slot(
    good=0, scrap=0, standard=11700,
    downtime=dt(planned=SLOT_MINUTES), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check("ppt", s["ppt_minutes"], 0)
check("oee is None", s["oee"], None)
check("availability is None", s["availability"], None)

print("\n== missing data yields None, never a default ==")
s = _compute_slot(
    good=9000, scrap=None, standard=11700,
    downtime=dt(unplanned=20), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check("oee survives missing scrap", s["oee"], 9000 / 15600)
check("availability survives", s["availability"], 100 / 120)
check("performance is None", s["performance"], None)
check("quality is None (not 1.0)", s["quality"], None)
check_eq("still counted for oee", s["counted"], True)

s = _compute_slot(
    good=9000, scrap=200, standard=11700,
    downtime=None, ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check("no downtime record -> oee None", s["oee"], None)
check("no downtime record -> availability None (not 1.0)", s["availability"], None)
check_eq("not counted", s["counted"], False)

s = _compute_slot(
    good=None, scrap=None, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("no units -> not counted", s["counted"], False)

print("\n== 'ran clean' is a header with no reasons, and is NOT missing ==")
s = _compute_slot(
    good=11700, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("has_downtime true for empty reasons", s["has_downtime"], True)
check("availability 100%", s["availability"], 1.0)

print("\n== impossible numbers are flagged and excluded ==")
s = _compute_slot(
    good=-500, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("negative delta flagged", s["flags"], ["negative_units"])
check_eq("negative delta not counted", s["counted"], False)

# Above the derived ceiling (15,600 for C1) but under double it. The ceiling is
# standard / 0.75 — a derived number resting on an unverified assumption — so
# the machine beating it is far more likely to mean the RATE is wrong than that
# the production is. It warns and still counts.
s = _compute_slot(
    good=20000, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("above ceiling is a warning, not a flag", s["flags"], [])
check_eq("above ceiling warns on the slot", s["warnings"], ["over_100"])
check_eq("above ceiling STILL COUNTS", s["counted"], True)
check("and keeps its real value, unclamped", s["oee"], 20000 / 15600)

# Over double the ceiling is a different claim: not a rate disagreement but a
# transposed digit or a day-cumulative value in a shift-cumulative box.
s = _compute_slot(
    good=40000, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("over 2x the ceiling is flagged", s["flags"], ["implausible_units"])
check_eq("and excluded", s["counted"], False)

# Right at the boundary, to pin the threshold down.
s = _compute_slot(
    good=31200, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("exactly 2x the ceiling still counts", s["counted"], True)
s = _compute_slot(
    good=31201, scrap=0, standard=11700,
    downtime=dt(), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("a unit past 2x is excluded", s["counted"], False)

s = _compute_slot(
    good=9000, scrap=0, standard=11700,
    downtime=dt(planned=60, unplanned=90), ideal_per_minute=IDEAL_C1, is_elapsed=True,
)
check_eq("downtime past slot flagged", s["flags"], ["downtime_over_slot"])
check_eq("downtime past slot not counted", s["counted"], False)

print("\n== _aggregate over a whole shift ==")
counted = [
    _compute_slot(good=g, scrap=sc, standard=11700, downtime=dt(unplanned=u),
                  ideal_per_minute=IDEAL_C1, is_elapsed=True)
    for g, sc, u in ((11000, 100, 10), (9000, 200, 20), (11700, 0, 0), (10500, 150, 15))
]
shift = _aggregate(
    good=sum(s["good"] for s in counted),
    scrap=sum(s["scrap"] for s in counted),
    standard=sum(s["standard"] for s in counted),
    ppt=sum(s["ppt_minutes"] for s in counted),
    run=sum(s["run_minutes"] for s in counted),
    planned=sum(s["planned_minutes"] for s in counted),
    unplanned=sum(s["unplanned_minutes"] for s in counted),
    ideal_at_ppt=sum(IDEAL_C1 * s["ppt_minutes"] for s in counted),
    ideal_at_run=sum(IDEAL_C1 * s["run_minutes"] for s in counted),
)
good, scrap = 42200, 450
check("shift good", shift["good"], good)
check("shift ppt", shift["ppt_minutes"], 480)
check("shift run", shift["run_minutes"], 480 - 45)
check("shift availability", shift["availability"], 435 / 480)
check("shift oee", shift["oee"], good / (130 * 480))
check("shift quality", shift["quality"], good / (good + scrap))
check("shift A x P x Q == oee", shift["oee_from_factors"], shift["oee"])
check("shift pct_of_standard", shift["pct_of_standard"], good / 46800)

print("\n== rollup sums components; it does NOT average percentages ==")
# A fast machine running well plus a slow machine running badly. The naive
# mean of the two OEEs is materially different from the correct rollup.
fast = _aggregate(good=46800, scrap=0, standard=46800, ppt=480, run=480,
                  planned=0, unplanned=0,
                  ideal_at_ppt=130 * 480, ideal_at_run=130 * 480)
slow = _aggregate(good=2000, scrap=0, standard=10080, ppt=480, run=240,
                  planned=0, unplanned=240,
                  ideal_at_ppt=28 * 480, ideal_at_run=28 * 240)
acc = _new_accumulator()
_accumulate(acc, fast, with_scrap=True)
_accumulate(acc, slow, with_scrap=True)
rollup = _aggregate(scrap=acc["scrap"], **_accumulator_args(acc))

naive_mean = (fast["oee"] + slow["oee"]) / 2
correct = (46800 + 2000) / (130 * 480 + 28 * 480)
check("fast machine oee", fast["oee"], 0.75)
check("slow machine oee", slow["oee"], 2000 / (28 * 480))
check("rollup oee is component-summed", rollup["oee"], correct)
print(f"        (naive mean would have been {naive_mean:.4f} vs correct {correct:.4f})")
check_eq("rollup differs from the mean", abs(rollup["oee"] - naive_mean) > 0.01, True)

# The whole reason availability is capacity-weighted rather than clock-based:
# with mixed rates, a clock-time availability makes the three factors multiply
# out to something that is not the rollup's OEE.
check("rollup A x P x Q == oee", rollup["oee_from_factors"], rollup["oee"])
check("rollup availability is capacity-weighted",
      rollup["availability"], (130 * 480 + 28 * 240) / (130 * 480 + 28 * 480))
check("rollup uptime is clock-based and differs",
      rollup["uptime"], 720 / 960)
check_eq("the two availabilities diverge on a rollup",
         abs(rollup["availability"] - rollup["uptime"]) > 0.01, True)

# ...and they must NOT diverge for a single machine, where the rate cancels.
check("single machine: availability == uptime", fast["availability"], fast["uptime"])
check("single machine slow: availability == uptime", slow["availability"], slow["uptime"])
check("single machine slow availability is run/ppt", slow["availability"], 240 / 480)

print("\n== _rank_pareto ==")
ranked = _rank_pareto({
    "MATL": {"code": "MATL", "label": "Material", "is_planned": False, "minutes": 90, "occurrences": 3},
    "MECH": {"code": "MECH", "label": "Mechanical", "is_planned": False, "minutes": 30, "occurrences": 1},
    "PM": {"code": "PM", "label": "Planned maint", "is_planned": True, "minutes": 200, "occurrences": 2},
})
check_eq("sorted by minutes desc", [r["code"] for r in ranked], ["PM", "MATL", "MECH"])
check("MATL share of unplanned", ranked[1]["pct_of_unplanned"], 90 / 120)
check("planned rows carry no unplanned share", ranked[0]["pct_of_unplanned"], None)

print("\n== elapsed_slots ==")
from datetime import date, datetime  # noqa: E402
check_eq("past date -> all four",
         elapsed_slots("1st Shift", date(2026, 9, 1), datetime(2026, 9, 3, 10, 0)),
         ["8AM", "10AM", "12PM", "2PM"])
check_eq("future date -> none",
         elapsed_slots("1st Shift", date(2026, 9, 4), datetime(2026, 9, 3, 10, 0)),
         [])
check_eq("today mid-shift -> only closed windows",
         elapsed_slots("1st Shift", date(2026, 9, 3), datetime(2026, 9, 3, 11, 30)),
         ["8AM", "10AM"])
check_eq("today, 2nd shift not started",
         elapsed_slots("2nd Shift", date(2026, 9, 3), datetime(2026, 9, 3, 11, 30)),
         [])
check_eq("today, 3rd shift already landed this morning",
         elapsed_slots("3rd Shift", date(2026, 9, 3), datetime(2026, 9, 3, 11, 30)),
         ["12AM", "2AM", "4AM", "6AM"])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
