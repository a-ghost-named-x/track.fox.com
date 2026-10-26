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
import itertools
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
    SHIFT_ORDER,
    SHIFT_SLOTS,
    TIME_SLOTS,
    day_conflict,
    day_conflicts,
    day_plan,
    default_production_day,
    default_scheduled,
    default_start,
    elapsed_dated_slots,
    elapsed_slots,
    shift_slot_dates,
    shift_span,
    start_options,
    window_span,
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

def plan_of(lengths=None, starts=None):
    return day_plan(lengths or {}, starts or {})


def spans(plan):
    return [shift_span(s, plan) for s in SHIFT_ORDER]


def slots_of(plan):
    return [plan[s]["slots"] for s in SHIFT_ORDER]


print("\n== shift geometry: each shift starts when the one before it ends ==")
check_eq("nothing set reproduces SHIFT_SLOTS",
         {s: p["slots"] for s, p in plan_of().items()}, SHIFT_SLOTS)
check_eq("spans at 8h", spans(plan_of()), ["6AM-2PM", "2PM-10PM", "10PM-6AM"])
check_eq("1st, 10h runs to 4PM", plan_of({"1st Shift": 10})["1st Shift"]["slots"],
         ["8AM", "10AM", "12PM", "2PM", "4PM"])
check_eq("...and the 2nd follows it at 10h: 4PM-2AM, then a 4h 3rd to 6AM",
         spans(plan_of({"1st Shift": 10})), ["6AM-4PM", "4PM-2AM", "2AM-6AM"])
check_eq("1st = 12 -> 6AM-6PM, 6PM-6AM, and no room for a 3rd",
         spans(plan_of({"1st Shift": 12})), ["6AM-6PM", "6PM-6AM", None])
check_eq("...the night owns 8PM to 6AM", plan_of({"1st Shift": 12})["2nd Shift"]["slots"],
         ["8PM", "10PM", "12AM", "2AM", "4AM", "6AM"])

print("\n== WS5/WS6: an 8h day, a 10h 2nd Shift from 2PM, a 3rd crew to 6AM ==")
ws5 = plan_of({"2nd Shift": 10})
check_eq("the 2nd Shift is 2PM-12AM, not 4PM-2AM", shift_span("2nd Shift", ws5), "2PM-12AM")
check_eq("...owning 4PM to 12AM", ws5["2nd Shift"]["slots"], ["4PM", "6PM", "8PM", "10PM", "12AM"])
check_eq("a 3rd Shift exists after it, 12AM-6AM", shift_span("3rd Shift", ws5), "12AM-6AM")
check_eq("...6 hours, the rest of the night", ws5["3rd Shift"]["hours"], 6)
check_eq("...owning 2AM to 6AM", ws5["3rd Shift"]["slots"], ["2AM", "4AM", "6AM"])
check_eq("the day fits", day_conflict(ws5), None)
check_eq("the 2nd and 3rd start unticked after a long shift",
         [default_scheduled(s, ws5) for s in SHIFT_ORDER], [True, False, False])
ws5_set = plan_of({"2nd Shift": 10, "3rd Shift": 6})
check_eq("setting the 3rd to 6h changes nothing else", spans(ws5_set), spans(ws5))
check_eq("an 8h 3rd won't fit after it",
         day_conflict(plan_of({"2nd Shift": 10, "3rd Shift": 8})),
         "3rd Shift (8h from 12AM) would run past 6AM. Make it 6h or less, or start it earlier.")

print("\n== a start can be set for a crew that came on later ==")
late10 = plan_of({"2nd Shift": 10}, {"2nd Shift": 16})
check_eq("a 10h crew from 4PM after an 8h day", spans(late10), ["6AM-2PM", "4PM-2AM", "2AM-6AM"])
check_eq("...owns 6PM to 2AM; the 4PM box is nobody's",
         late10["2nd Shift"]["slots"], ["6PM", "8PM", "10PM", "12AM", "2AM"])
late12 = plan_of({"2nd Shift": 12}, {"2nd Shift": 18})
check_eq("a 12h crew from 6PM after an 8h day is the 6PM-6AM night",
         spans(late12), ["6AM-2PM", "6PM-6AM", None])
check_eq("a 12h crew after an 8h day starts at 2PM unless set",
         spans(plan_of({"2nd Shift": 12})), ["6AM-2PM", "2PM-2AM", "2AM-6AM"])
check_eq("a 3rd crew from 2AM after an ordinary 2nd Shift",
         spans(plan_of({"3rd Shift": 4}, {"3rd Shift": 2})), ["6AM-2PM", "2PM-10PM", "2AM-6AM"])
check_eq("...the 2nd still owns its own four boxes",
         plan_of({"3rd Shift": 4}, {"3rd Shift": 2})["2nd Shift"]["slots"], SHIFT_SLOTS["2nd Shift"])
check_eq("start options for a 2nd Shift after an 8h day: 2PM to 4AM",
         start_options("2nd Shift", plan_of()), [14, 16, 18, 20, 22, 0, 2, 4])
check_eq("...after a 12h day: 6PM to 4AM",
         start_options("2nd Shift", plan_of({"1st Shift": 12})), [18, 20, 22, 0, 2, 4])
check_eq("a 3rd Shift's options start when the 2nd Shift is over",
         start_options("3rd Shift", ws5), [0, 2, 4])
check_eq("...none after a 2nd that runs until 6AM",
         start_options("3rd Shift", plan_of({"1st Shift": 12})), [])
check_eq("the 1st Shift has no start to set", start_options("1st Shift", plan_of()), [])
check_eq("the default start ignores the shift's own setting",
         default_start("2nd Shift", {"2nd Shift": 10}, {"2nd Shift": 16}), 14)
check_eq("...and follows a long 1st Shift",
         default_start("2nd Shift", {"1st Shift": 10}, {}), 16)

print("\n== the day has to fit ==")
for lengths, starts in (({}, {}), ({"1st Shift": 12}, {}), ({"2nd Shift": 12}, {}),
                        ({"1st Shift": 10, "2nd Shift": 12}, {}), ({"2nd Shift": 10}, {}),
                        ({"1st Shift": 12, "2nd Shift": 8}, {}),
                        ({"2nd Shift": 12}, {"2nd Shift": 18})):
    check_eq(f"{lengths or '8/8/8'} {starts or ''} is fine", day_conflict(plan_of(lengths, starts)), None)
msg = day_conflict(plan_of({"1st Shift": 12}, {"2nd Shift": 16}))
check_eq("a 2nd Shift can't start inside the 1st", msg,
         "2nd Shift can't start at 4PM: 1st Shift (12h, 6AM-6PM) runs until 6PM. "
         "Start 2nd Shift at 6PM or later, or shorten 1st Shift.")
check_eq("a short 1st Shift's crew can be there until 2PM",
         day_conflict(plan_of({"1st Shift": 4}, {"2nd Shift": 12})) is not None, True)
check_eq("a 12h night can't start at 8PM (past 6AM)",
         day_conflict(plan_of({"2nd Shift": 12}, {"2nd Shift": 20})),
         "2nd Shift (12h from 8PM) would run past 6AM. Make it 10h or less, or start it earlier.")
check_eq("a 3rd crew can't start before the 2nd is over",
         day_conflict(plan_of({"2nd Shift": 10}, {"3rd Shift": 22})) is not None, True)

print("\n== a short shift sits inside its window ==")
short2 = plan_of({"2nd Shift": 6})
check_eq("a 6h 2nd Shift is 2PM-8PM", shift_span("2nd Shift", short2), "2PM-8PM")
check_eq("...inside 2PM-10PM", window_span("2nd Shift", short2), "2PM-10PM")
check_eq("...so the 3rd still starts at 10PM", shift_span("3rd Shift", short2), "10PM-6AM")
cut = plan_of({"2nd Shift": 4, "3rd Shift": 8}, {"3rd Shift": 18})
check_eq("a crew set to start inside the window cuts it short",
         (window_span("2nd Shift", cut), cut["2nd Shift"]["slots"]), ("2PM-6PM", ["4PM", "6PM"]))
check_eq("...and owns the rest", cut["3rd Shift"]["slots"],
         ["8PM", "10PM", "12AM", "2AM"])
check_eq("a short 1st Shift has no fixed start", shift_span("1st Shift", plan_of({"1st Shift": 4})),
         "within 6AM-2PM")
check_eq("...inside the 6AM-2PM shift", window_span("1st Shift", plan_of({"1st Shift": 4})),
         "6AM-2PM")

print("\n== a short shift after a 10 or 12-hour 1st Shift sits inside the long night ==")
night = plan_of({"1st Shift": 12, "2nd Shift": 4})
check_eq("12h then 4h is a valid day", day_conflict(night), None)
check_eq("the 4h crew starts when the day crew leaves", shift_span("2nd Shift", night), "6PM-10PM")
check_eq("...and owns the night's checkpoints", night["2nd Shift"]["slots"],
         ["8PM", "10PM", "12AM", "2AM", "4AM", "6AM"])
check_eq("so there is no 3rd Shift unless one is given a start", night["3rd Shift"]["slots"], None)
check_eq("after a 10h 1st, 4 hours is 4PM-8PM",
         shift_span("2nd Shift", plan_of({"1st Shift": 10, "2nd Shift": 4})), "4PM-8PM")
check_eq("a 4h night crew starts unticked; there is no 3rd Shift",
         [default_scheduled(s, night) for s in SHIFT_ORDER], [True, False, False])
check_eq("a 3rd crew from 10PM fits after the 4h crew",
         spans(plan_of({"1st Shift": 12, "2nd Shift": 4}, {"3rd Shift": 22})),
         ["6AM-6PM", "6PM-10PM", "10PM-6AM"])
msg = day_conflict(plan_of({"1st Shift": 12, "2nd Shift": 4, "3rd Shift": 8}))
check_eq("an 8h 3rd Shift with no start has no room in the 6PM-6AM night",
         msg is not None and "inside 6PM-6AM" in msg and "has no room" in msg, True)
check_eq("...and the fix named is the 12h 1st Shift", "shorten 1st Shift" in (msg or ""), True)

print("\n== every day the old rules accepted reads exactly as before ==")
# Before start times, the Nth L-hour shift started at 6AM + N x L and a later
# shift couldn't be shorter than the one before. Every day those rules
# accepted must keep its checkpoints, spans and Scheduled defaults, except a
# 10/12h 2nd Shift longer than the 1st Shift's window, which now starts when
# the 1st ends unless its old start is stored.


def old_window(hours):
    return max(hours, 8)


def old_resolve(explicit):
    lengths, window = {}, 8
    for label in SHIFT_ORDER:
        lengths[label] = explicit.get(label, window)
        if lengths[label] >= 8:
            window = lengths[label]
    return lengths


def old_windows(lengths):
    windows, window = {}, 8
    for label in SHIFT_ORDER:
        if lengths[label] >= 8:
            window = lengths[label]
        windows[label] = window
    return windows


def old_slots(shift, window):
    per_shift = window // 2
    index = SHIFT_ORDER.index(shift)
    chunk = TIME_SLOTS[index * per_shift:(index + 1) * per_shift]
    return chunk if len(chunk) == per_shift else None


def label_of(h):
    return f"{(h % 12) or 12}{'AM' if h % 24 < 12 else 'PM'}"


def old_span(shift, hours, window):
    if old_slots(shift, window) is None:
        return None
    if hours < 8 and shift == SHIFT_ORDER[0]:
        return f"within {old_span(shift, window, window)}"
    start = 6 + SHIFT_ORDER.index(shift) * window
    return f"{label_of(start)}-{label_of(start + hours)}"


def old_default_scheduled(shift, lengths, windows):
    if old_slots(shift, windows[shift]) is None:
        return False
    index = SHIFT_ORDER.index(shift)
    return index == 0 or all(lengths[s] == 8 for s in SHIFT_ORDER[:index + 1])


compared = moved = mismatched = 0
for combo in itertools.product([None, 2, 4, 6, 8, 10, 12], [None, 2, 4, 6, 8, 10, 12],
                               [None, 2, 4, 6, 8]):
    explicit = {s: h for s, h in zip(SHIFT_ORDER, combo) if h is not None}
    old = old_resolve(explicit)
    windows = old_windows(old)
    if any(windows[c] < windows[p] for p, c in zip(SHIFT_ORDER, SHIFT_ORDER[1:])):
        continue  # the old rules refused this day
    compared += 1
    starts = {}
    changed = old["2nd Shift"] >= 10 and old["2nd Shift"] > windows["1st Shift"]
    if changed:
        moved += 1
        # The old start, as 21_keep_old_shift_starts.sql stores it.
        starts = {"2nd Shift": (6 + old["2nd Shift"]) % 24}
    new = day_plan(explicit, starts)
    # A length typed on a 3rd Shift the old rules had no room for meant
    # nothing then, and may not fit now. That's the only conflict allowed.
    absent = [s for s in SHIFT_ORDER if old_slots(s, windows[s]) is None]
    same = all(
        any(message.startswith(s) for s in absent) for message in day_conflicts(new)
    ) and all(
        new[s]["slots"] == old_slots(s, windows[s])
        and shift_span(s, new) == old_span(s, old[s], windows[s])
        and default_scheduled(s, new) == old_default_scheduled(s, old, windows)
        for s in SHIFT_ORDER
        # A 3rd Shift the old rules had no room for may exist now, but only
        # ever unticked, so it changes no number.
        if old_slots(s, windows[s]) is not None
    ) and all(
        not default_scheduled(s, new)
        for s in SHIFT_ORDER if old_slots(s, windows[s]) is None
    )
    if not same:
        mismatched += 1
        print(f"    differs: {explicit} {starts}")
check_eq(f"all {compared} days the old rules accepted are unchanged "
         f"({moved} of them with their old start stored)", mismatched, 0)
check_eq("the days that move are 8->10, 8->12 and 10->12 (with or without a short 3rd)",
         moved > 0, True)

print("\n== the production day and the rounds' calendar dates ==")
# 12AM-6AM checkpoints carry the next calendar date, as the rounds file them.
check_eq("3rd Shift of 9/18 lands on 9/19",
         shift_slot_dates("3rd Shift", date(2026, 9, 18)),
         [(s, date(2026, 9, 19)) for s in ("12AM", "2AM", "4AM", "6AM")])
check_eq("12h 2nd Shift of 9/15 straddles midnight",
         shift_slot_dates("2nd Shift", date(2026, 9, 15), plan_of({"1st Shift": 12})),
         [("8PM", date(2026, 9, 15)), ("10PM", date(2026, 9, 15)),
          ("12AM", date(2026, 9, 16)), ("2AM", date(2026, 9, 16)),
          ("4AM", date(2026, 9, 16)), ("6AM", date(2026, 9, 16))])
check_eq("a shift the day has no room for is None",
         shift_slot_dates("3rd Shift", date(2026, 9, 15), plan_of({"1st Shift": 12})), None)
check_eq("elapsed follows each slot's own date",
         elapsed_dated_slots(shift_slot_dates("2nd Shift", date(2026, 9, 15),
                                              plan_of({"1st Shift": 12})),
                             datetime(2026, 9, 16, 2, 0)),
         ["8PM", "10PM", "12AM", "2AM"])

print("\n== defaults ==")
check_eq("8h: everything scheduled by default",
         [default_scheduled(s, plan_of()) for s in SHIFT_SLOTS], [True, True, True])
check_eq("12h: 1st yes, 2nd no (night crew is the exception), 3rd doesn't exist",
         [default_scheduled(s, plan_of({"1st Shift": 12})) for s in SHIFT_SLOTS],
         [True, False, False])
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
