"""Verifies the pure OEE arithmetic in app/db/oee.py.

Run it directly (there's no test framework):

    python tests/test_oee_math.py

Exits non-zero on any failure.

The key assertion is that A x P x Q equals OEE computed the short way
(good / (ideal rate x PPT)) for both single machines and rollups. If that
fails, the aggregation is wrong.

No database is touched. Importing the functions pulls in app.db.postgres,
which needs psycopg and a password, hence the two shims. The psycopg shim only
applies if the real driver isn't installed.
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
    default_production_day,
    default_scheduled,
    elapsed_dated_slots,
    elapsed_slots,
    resolve_day_lengths,
    shift_length_conflict,
    shift_plan,
    shift_slot_dates,
    shift_slots,
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

# A real shift: operator moved at 12PM, so 2PM was left blank because the
# count hadn't changed. Treating the blank as unknown gives 33.7%; counting it
# as zero gives the correct 25.2%.
check_eq("trailing blank produces zero, not unknown",
         _cumulative_deltas(readings(10500, 15750, 15750, None), SLOTS, elapsed=ALL),
         {"8AM": 10500, "10AM": 5250, "12PM": 0, "2PM": 0})
check_eq("blank first slot means the counter was still at zero",
         _cumulative_deltas(readings(None, 15750, None, None), SLOTS, elapsed=ALL),
         {"8AM": 0, "10AM": 15750, "12PM": 0, "2PM": 0})
check_eq("interior blank absorbed, later reading still correct",
         _cumulative_deltas(readings(11700, None, 35100, 46800), SLOTS, elapsed=ALL),
         {"8AM": 11700, "10AM": 0, "12PM": 23400, "2PM": 11700})

# Guard 1: a shift with no readings is unknown, not zero.
check_eq("a shift with no readings stays unknown",
         _cumulative_deltas({}, SLOTS, elapsed=ALL),
         {"8AM": None, "10AM": None, "12PM": None, "2PM": None})
# Guard 2: slots that haven't happened yet are unknown.
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

# A low mid-shift reading that a later one recovers from: the total is still
# right, so it warns rather than excluding the shift.
r = readings(11700, 23400, 20000, 46800)
p = _summarise_production(r, _cumulative_deltas(r, SLOTS, elapsed=ALL), SLOTS)
check_eq("mid-shift typo warns", p["warnings"], ["units_went_backwards"])
check_eq("mid-shift typo is NOT a hard flag", p["flags"], [])
check("...and the shift total is still the last reading", p["good"], 46800)

# A last reading below an earlier one makes the total wrong, so it's excluded.
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
# No reason codes are planned today, but the arithmetic must still handle it.
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

# Why availability is capacity-weighted: with mixed rates, clock-time
# availability makes A x P x Q differ from the rollup's OEE.
check("rollup A x P x Q == oee", rollup["oee_from_factors"], rollup["oee"])
check("rollup availability is capacity-weighted",
      rollup["availability"], (130 * 480 + 28 * 240) / (130 * 480 + 28 * 480))
check("rollup uptime is clock-based and differs", rollup["uptime"], 720 / 960)
check_eq("the two availabilities diverge on a rollup",
         abs(rollup["availability"] - rollup["uptime"]) > 0.01, True)
# ...but for a single machine the two are the same.
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
# The date is the production day: 3rd Shift of 9/3 is 9/3 10PM -> 9/4 6AM.
check_eq("today at 11:30, tonight's 3rd shift hasn't started",
         elapsed_slots("3rd Shift", date(2026, 9, 3), datetime(2026, 9, 3, 11, 30)), [])
check_eq("3rd shift of 9/2 landed this morning",
         elapsed_slots("3rd Shift", date(2026, 9, 2), datetime(2026, 9, 3, 11, 30)),
         ["12AM", "2AM", "4AM", "6AM"])
check_eq("3rd shift of 9/3 at 3:30AM on 9/4: two slots in",
         elapsed_slots("3rd Shift", date(2026, 9, 3), datetime(2026, 9, 4, 3, 30)),
         ["12AM", "2AM"])

print("\n== shift length geometry: the Nth L-hour shift starts at 6AM + N x L ==")
check_eq("8h reproduces SHIFT_SLOTS", shift_plan(8), SHIFT_SLOTS)
check_eq("1st, 10h runs to 4PM", shift_slots("1st Shift", 10), ["8AM", "10AM", "12PM", "2PM", "4PM"])
check_eq("2nd, 10h is 4PM-2AM", shift_slots("2nd Shift", 10), ["6PM", "8PM", "10PM", "12AM", "2AM"])
check_eq("3rd, 10h has no room", shift_slots("3rd Shift", 10), None)
check_eq("1st, 12h runs to 6PM", shift_slots("1st Shift", 12),
         ["8AM", "10AM", "12PM", "2PM", "4PM", "6PM"])
check_eq("2nd, 12h is 6PM-6AM (never 2PM-2AM)", shift_slots("2nd Shift", 12),
         ["8PM", "10PM", "12AM", "2AM", "4AM", "6AM"])
check_eq("3rd, 12h has no room", shift_slots("3rd Shift", 12), None)
check_eq("spans at 12h", [shift_span(s, 12) for s in SHIFT_SLOTS], ["6AM-6PM", "6PM-6AM", None])
check_eq("spans at 10h", [shift_span(s, 10) for s in SHIFT_SLOTS], ["6AM-4PM", "4PM-2AM", None])
check_eq("spans at 8h", [shift_span(s, 8) for s in SHIFT_SLOTS], ["6AM-2PM", "2PM-10PM", "10PM-6AM"])

print("\n== each shift inherits the one before it ==")
check_eq("nothing set -> 8/8/8", resolve_day_lengths({}),
         {"1st Shift": 8, "2nd Shift": 8, "3rd Shift": 8})
check_eq("1st = 12 -> the night follows", resolve_day_lengths({"1st Shift": 12}),
         {"1st Shift": 12, "2nd Shift": 12, "3rd Shift": 12})
# 8-hour 1st Shift, idle 2PM-6PM, then a 12-hour crew at 6PM.
check_eq("2nd = 12 on its own", resolve_day_lengths({"2nd Shift": 12}),
         {"1st Shift": 8, "2nd Shift": 12, "3rd Shift": 12})
check_eq("...so 1st is 6AM-2PM and 2nd is 6PM-6AM",
         [shift_span(s, h) for s, h in resolve_day_lengths({"2nd Shift": 12}).items()],
         ["6AM-2PM", "6PM-6AM", None])

print("\n== the as-long-or-longer rule ==")
for combo in ({}, {"1st Shift": 12}, {"2nd Shift": 12}, {"1st Shift": 10, "2nd Shift": 12},
              {"2nd Shift": 10}, {"1st Shift": 10}):
    check_eq(f"{combo or '8/8/8'} is fine",
             shift_length_conflict(resolve_day_lengths(combo)), None)
msg = shift_length_conflict(resolve_day_lengths({"1st Shift": 12, "2nd Shift": 8}))
check_eq("12 then 8 overlaps", msg is not None, True)
check_eq("...and the message names both shifts",
         "2nd Shift" in (msg or "") and "1st Shift" in (msg or "") and "6AM-6PM" in (msg or ""), True)
check_eq("12 then 10 overlaps too",
         shift_length_conflict(resolve_day_lengths({"1st Shift": 12, "2nd Shift": 10})) is not None,
         True)
check_eq("a 3rd Shift explicitly shorter than a long 2nd is caught",
         shift_length_conflict(resolve_day_lengths({"2nd Shift": 12, "3rd Shift": 8})) is not None,
         True)

print("\n== the production day and the rounds' calendar dates ==")
# 12AM-6AM checkpoints carry the next calendar date, as the rounds file them.
check_eq("3rd Shift of 9/18 lands on 9/19",
         shift_slot_dates("3rd Shift", date(2026, 9, 18)),
         [(s, date(2026, 9, 19)) for s in ("12AM", "2AM", "4AM", "6AM")])
check_eq("12h 2nd Shift of 9/15 straddles midnight",
         shift_slot_dates("2nd Shift", date(2026, 9, 15), 12),
         [("8PM", date(2026, 9, 15)), ("10PM", date(2026, 9, 15)),
          ("12AM", date(2026, 9, 16)), ("2AM", date(2026, 9, 16)),
          ("4AM", date(2026, 9, 16)), ("6AM", date(2026, 9, 16))])
check_eq("a shift the day has no room for is None",
         shift_slot_dates("3rd Shift", date(2026, 9, 15), 12), None)
check_eq("elapsed follows each slot's own date",
         elapsed_dated_slots(shift_slot_dates("2nd Shift", date(2026, 9, 15), 12),
                             datetime(2026, 9, 16, 2, 0)),
         ["8PM", "10PM", "12AM", "2AM"])

print("\n== defaults ==")
check_eq("8h: everything scheduled by default",
         [default_scheduled(s, 8) for s in SHIFT_SLOTS], [True, True, True])
check_eq("12h: 1st yes, 2nd no (night crew is the exception), 3rd doesn't exist",
         [default_scheduled(s, 12) for s in SHIFT_SLOTS], [True, False, False])
# The form's date box: 3rd Shift is entered the morning after it started.
check_eq("3rd Shift page at 6:30AM Fri -> Thu",
         default_production_day("3rd Shift", datetime(2026, 9, 19, 6, 30)), date(2026, 9, 18))
check_eq("3rd Shift page at 11PM Thu -> Thu (tonight's)",
         default_production_day("3rd Shift", datetime(2026, 9, 18, 23, 0)), date(2026, 9, 18))
check_eq("1st Shift page at 6:30AM Fri -> Fri",
         default_production_day("1st Shift", datetime(2026, 9, 19, 6, 30)), date(2026, 9, 19))
check_eq("2nd Shift page at 3AM Fri -> Thu (the running day)",
         default_production_day("2nd Shift", datetime(2026, 9, 19, 3, 0)), date(2026, 9, 18))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
