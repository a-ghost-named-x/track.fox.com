"""Verifies the pure OEE arithmetic in app/db/oee.py.

Run it directly — there's no test framework in this project:

    python tests/test_oee_math.py

Exits non-zero with a list of failures, so it works as a pre-push check.

The single most important assertion in here is that A x P x Q equals OEE
computed the short way (good / (ideal rate x PPT)) at BOTH the machine and the
rollup level. That identity is what caught the capacity-vs-clock-time
availability bug; if it fails again, the aggregation is wrong.

Nothing here touches a database. Only pure functions are exercised — but
importing them pulls in app.db.postgres, which imports psycopg at module scope
and needs a password from config, hence the two shims. The psycopg shim only
fires if the real driver is absent, so it cannot shadow the genuine module.
"""
import os
import sys
import types
from datetime import date, datetime
from pathlib import Path

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
    _cumulative_deltas,
    _machine_warnings,
    _new_accumulator,
    _rank_pareto,
    _safe_divide,
    _summarise_production,
)
from app.models import (  # noqa: E402
    SHIFT_MINUTES,
    SHIFT_SLOTS,
    default_scheduled,
    elapsed_dated_slots,
    elapsed_slots,
    production_day_for,
    shift_plan,
    shift_slot_dates,
    shift_span,
)

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


SLOTS = SHIFT_SLOTS["1st Shift"]
ALL = set(SLOTS)
IDEAL_C1 = 130.0  # units/minute = 7800/hr, from a standard of 11,700 per slot


def readings(*values):
    return dict(zip(SLOTS, values))


def shift_of(good, scrap, unplanned, ideal=IDEAL_C1, standard=46800, planned=0):
    """One machine's whole-shift aggregate, the way compute_oee_report builds it."""
    ppt = SHIFT_MINUTES - planned
    run = ppt - unplanned
    return _aggregate(
        good=good, scrap=scrap, standard=standard,
        ppt=ppt, run=run, planned=planned, unplanned=unplanned,
        ideal_at_ppt=ideal * ppt, ideal_at_run=ideal * run,
    )


print("\n== _safe_divide never defaults ==")
check("zero denominator", _safe_divide(5, 0), None)
check("None numerator", _safe_divide(None, 5), None)
check("None denominator", _safe_divide(5, None), None)
check("normal", _safe_divide(3, 4), 0.75)

print("\n== _cumulative_deltas: a blank checkpoint means UNCHANGED ==")
check_eq("clean ramp",
         _cumulative_deltas(readings(11700, 23400, 35100, 46800), SLOTS, elapsed=ALL),
         {"8AM": 11700, "10AM": 11700, "12PM": 11700, "2PM": 11700})

# C2, 2026-09-03. Operator moved to WS at 12PM, so 2PM was left blank because
# the number hadn't moved. Treating that as unknown scored the machine 33.7%
# when the truth is 25.2% — and flattered it precisely because it had a bad day.
check_eq("trailing blank produces zero, not unknown",
         _cumulative_deltas(readings(10500, 15750, 15750, None), SLOTS, elapsed=ALL),
         {"8AM": 10500, "10AM": 5250, "12PM": 0, "2PM": 0})
check_eq("blank first slot means the counter was still at zero",
         _cumulative_deltas(readings(None, 15750, None, None), SLOTS, elapsed=ALL),
         {"8AM": 0, "10AM": 15750, "12PM": 0, "2PM": 0})
check_eq("interior blank absorbed, later reading still correct",
         _cumulative_deltas(readings(11700, None, 35100, 46800), SLOTS, elapsed=ALL),
         {"8AM": 11700, "10AM": 0, "12PM": 23400, "2PM": 11700})

# GUARD 1: total silence is not a claim of zero production.
check_eq("a shift with no readings stays unknown",
         _cumulative_deltas({}, SLOTS, elapsed=ALL),
         {"8AM": None, "10AM": None, "12PM": None, "2PM": None})
# GUARD 2: "unchanged" is meaningless for hours that haven't happened.
check_eq("mid-shift, un-elapsed slots stay unknown rather than zero",
         _cumulative_deltas(readings(11700, 23400, None, None), SLOTS,
                            elapsed={"8AM", "10AM"}),
         {"8AM": 11700, "10AM": 11700, "12PM": None, "2PM": None})

print("\n== _summarise_production: the shift total is the LAST reading ==")
for label, values in (
    ("all four", (10500, 15750, 15750, 15750)),
    ("trailing blank", (10500, 15750, 15750, None)),
    ("leading blank", (None, 15750, None, None)),
    ("one reading only", (15750, None, None, None)),
    ("interior blank", (10500, None, 15750, None)),
):
    r = readings(*values)
    d = _cumulative_deltas(r, SLOTS, elapsed=ALL)
    check(f"total survives a {label}", _summarise_production(r, d, SLOTS)["good"], 15750)

# A checkpoint typed low that a LATER reading recovers from: the reading needs
# fixing, but the shift total is the last one and is still right, so it warns
# rather than excluding a whole shift over a mid-shift typo.
r = readings(11700, 23400, 20000, 46800)
p = _summarise_production(r, _cumulative_deltas(r, SLOTS, elapsed=ALL), SLOTS)
check_eq("mid-shift typo warns", p["warnings"], ["units_went_backwards"])
check_eq("mid-shift typo is NOT a hard flag", p["flags"], [])
check("...and the shift total is still the last reading", p["good"], 46800)

# A LAST reading below an earlier one is different in kind: the shift total
# itself is understated, so nothing derived from it can be trusted.
r = readings(11700, 23400, 35100, 20000)
p = _summarise_production(r, _cumulative_deltas(r, SLOTS, elapsed=ALL), SLOTS)
check_eq("final reading low is a hard flag", p["flags"], ["final_reading_low"])

r = readings(None, None, None, None)
p = _summarise_production(r, _cumulative_deltas(r, SLOTS, elapsed=ALL), SLOTS)
check_eq("nothing reported -> good is None, not zero", p["good"], None)

print("\n== the 75% anchor: at standard, no losses ==")
s = shift_of(good=46800, scrap=0, unplanned=0)
check("oee at standard is exactly 0.75", s["oee"], 0.75)
check("availability", s["availability"], 1.0)
check("quality", s["quality"], 1.0)
check("performance", s["performance"], 0.75)
check("pct_of_standard", s["pct_of_standard"], 1.0)
check("A x P x Q == oee", s["oee_from_factors"], s["oee"])

print("\n== headroom: beating standard reaches 80/90/100% ==")
for good, want in ((49920, 0.80), (53040, 0.85), (56160, 0.90), (62400, 1.00)):
    check(f"good {good} -> oee", shift_of(good, 0, 0)["oee"], want)

print("\n== C2, the real row: blank tail plus 240 min of no operator ==")
c2 = shift_of(good=15750, scrap=250, unplanned=240)
check("oee", c2["oee"], 15750 / (130 * 480))
check("availability is 240 of 480", c2["availability"], 0.5)
check("quality", c2["quality"], 15750 / 16000)
check("A x P x Q == oee", c2["oee_from_factors"], c2["oee"])

print("\n== planned downtime comes OUT of the denominator ==")
# No planned reason codes exist today, but the arithmetic must still hold so
# that adding one later needs no change to the maths.
p = shift_of(good=24000, scrap=0, unplanned=60, planned=120)
check("ppt excludes planned", p["ppt_minutes"], 360)
check("run excludes both", p["run_minutes"], 300)
check("availability is run/ppt not run/480", p["availability"], 300 / 360)
check("oee scaled to ppt", p["oee"], 24000 / (130 * 360))
check("A x P x Q == oee", p["oee_from_factors"], p["oee"])

print("\n== missing scrap costs P and Q, never OEE ==")
s = shift_of(good=40000, scrap=None, unplanned=60)
check("oee survives missing scrap", s["oee"], 40000 / (130 * 480))
check("availability survives", s["availability"], 420 / 480)
check("performance is None", s["performance"], None)
check("quality is None, not 1.0", s["quality"], None)

print("\n== rollup sums components; it does NOT average percentages ==")
fast = shift_of(good=46800, scrap=0, unplanned=0, ideal=130, standard=46800)
slow = _aggregate(good=2000, scrap=0, standard=10080, ppt=480, run=240,
                  planned=0, unplanned=240,
                  ideal_at_ppt=28 * 480, ideal_at_run=28 * 240)
acc = _new_accumulator()
_accumulate(acc, fast, with_scrap=True)
_accumulate(acc, slow, with_scrap=True)
rollup = _aggregate(scrap=acc["scrap"], **_accumulator_args(acc))

naive_mean = (fast["oee"] + slow["oee"]) / 2
correct = (46800 + 2000) / (130 * 480 + 28 * 480)
check("rollup oee is component-summed", rollup["oee"], correct)
print(f"        (naive mean would have been {naive_mean:.4f} vs correct {correct:.4f})")
check_eq("rollup differs from the mean", abs(rollup["oee"] - naive_mean) > 0.01, True)

# The whole reason availability is capacity-weighted rather than clock-based:
# with mixed rates a clock-time availability makes the three factors multiply
# out to something that is not the rollup's OEE.
check("rollup A x P x Q == oee", rollup["oee_from_factors"], rollup["oee"])
check("rollup availability is capacity-weighted",
      rollup["availability"], (130 * 480 + 28 * 240) / (130 * 480 + 28 * 480))
check("rollup uptime is clock-based and differs", rollup["uptime"], 720 / 960)
check_eq("the two availabilities diverge on a rollup",
         abs(rollup["availability"] - rollup["uptime"]) > 0.01, True)
# ...and must NOT diverge for a single machine, where the rate cancels.
check("single machine: availability == uptime", slow["availability"], slow["uptime"])

print("\n== _machine_warnings ==")
check_eq("no result, no warnings", _machine_warnings(None, []), [])
check_eq("production warnings carry through",
         _machine_warnings(None, ["units_went_backwards"]), ["units_went_backwards"])
check_eq("under the ceiling is quiet", _machine_warnings(shift_of(46800, 0, 0), []), [])
check_eq("over the ceiling warns",
         _machine_warnings(shift_of(70000, 0, 0), []), ["over_100"])

print("\n== _rank_pareto ==")
ranked = _rank_pareto({
    "LACK_MAT": {"code": "LACK_MAT", "label": "Lack of Material", "is_planned": False, "minutes": 90, "machines": 3},
    "EQUIP_FAIL": {"code": "EQUIP_FAIL", "label": "Equipment Failure", "is_planned": False, "minutes": 30, "machines": 1},
    "PM": {"code": "PM", "label": "Planned maintenance", "is_planned": True, "minutes": 200, "machines": 2},
})
check_eq("sorted by minutes desc", [r["code"] for r in ranked],
         ["PM", "LACK_MAT", "EQUIP_FAIL"])
check("share is of UNPLANNED total", ranked[1]["pct_of_unplanned"], 90 / 120)
check("planned rows carry no unplanned share", ranked[0]["pct_of_unplanned"], None)

print("\n== elapsed_slots ==")
check_eq("past date -> all four",
         elapsed_slots("1st Shift", date(2026, 9, 1), datetime(2026, 9, 3, 10, 0)),
         ["8AM", "10AM", "12PM", "2PM"])
check_eq("future date -> none",
         elapsed_slots("1st Shift", date(2026, 9, 4), datetime(2026, 9, 3, 10, 0)), [])
check_eq("today mid-shift -> only closed windows",
         elapsed_slots("1st Shift", date(2026, 9, 3), datetime(2026, 9, 3, 11, 30)),
         ["8AM", "10AM"])
check_eq("today, 3rd shift already landed this morning",
         elapsed_slots("3rd Shift", date(2026, 9, 3), datetime(2026, 9, 3, 11, 30)),
         ["12AM", "2AM", "4AM", "6AM"])

print("\n== shift length geometry: the day starts at 6AM and never mixes ==")
check_eq("8h reproduces SHIFT_SLOTS", shift_plan(8), SHIFT_SLOTS)
check_eq("10h: 1st runs to 4PM",
         shift_plan(10)["1st Shift"], ["8AM", "10AM", "12PM", "2PM", "4PM"])
check_eq("10h: 2nd runs 4PM-2AM", shift_plan(10)["2nd Shift"],
         ["6PM", "8PM", "10PM", "12AM", "2AM"])
check_eq("10h: no 3rd, and 2AM-6AM is idle", "3rd Shift" in shift_plan(10), False)
check_eq("12h: 1st runs to 6PM",
         shift_plan(12)["1st Shift"], ["8AM", "10AM", "12PM", "2PM", "4PM", "6PM"])
check_eq("12h: 2nd runs 6PM-6AM", shift_plan(12)["2nd Shift"],
         ["8PM", "10PM", "12AM", "2AM", "4AM", "6AM"])
check_eq("12h: no 3rd", "3rd Shift" in shift_plan(12), False)
check_eq("spans", [shift_span(s, 12) for s in ("1st Shift", "2nd Shift", "3rd Shift")],
         ["6AM-6PM", "6PM-6AM", None])
check_eq("10h spans", [shift_span(s, 10) for s in ("1st Shift", "2nd Shift")],
         ["6AM-4PM", "4PM-2AM"])

# The overnight shift is filed under the morning it lands on, so the 3rd Shift
# row of the 16th belongs to the 15th's production day.
check_eq("3rd Shift filed 9/16 is production day 9/15",
         production_day_for("3rd Shift", date(2026, 9, 16)), date(2026, 9, 15))
check_eq("1st Shift is the identity",
         production_day_for("1st Shift", date(2026, 9, 16)), date(2026, 9, 16))

# A 12-hour 2nd Shift straddles midnight: 8PM/10PM on the day, the rest the
# morning after — exactly how the 2-hour rounds already date them.
check_eq("12h 2nd Shift checkpoints carry two dates",
         shift_slot_dates("2nd Shift", date(2026, 9, 15), 12),
         [("8PM", date(2026, 9, 15)), ("10PM", date(2026, 9, 15)),
          ("12AM", date(2026, 9, 16)), ("2AM", date(2026, 9, 16)),
          ("4AM", date(2026, 9, 16)), ("6AM", date(2026, 9, 16))])
check_eq("a shift the pattern lacks is None",
         shift_slot_dates("3rd Shift", date(2026, 9, 15), 12), None)
check_eq("8h 3rd Shift of production day 9/15 lands on 9/16",
         shift_slot_dates("3rd Shift", date(2026, 9, 15)),
         [(s, date(2026, 9, 16)) for s in ("12AM", "2AM", "4AM", "6AM")])
check_eq("elapsed follows each slot's own date",
         elapsed_dated_slots(shift_slot_dates("2nd Shift", date(2026, 9, 15), 12),
                             datetime(2026, 9, 16, 2, 0)),
         ["8PM", "10PM", "12AM", "2AM"])

# A night crew on a long day is the exception, so it has to be ticked on.
check_eq("8h: everything scheduled by default",
         [default_scheduled(s, 8) for s in SHIFT_SLOTS], [True, True, True])
check_eq("12h: 1st yes, 2nd no, 3rd doesn't exist",
         [default_scheduled(s, 12) for s in SHIFT_SLOTS], [True, False, False])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
