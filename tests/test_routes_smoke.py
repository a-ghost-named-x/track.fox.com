"""End-to-end smoke test: real routes, real Jinja rendering, stubbed database.

Run it from the REPO ROOT — Jinja2Templates resolves "app/templates"
relatively, so it won't find the templates from anywhere else:

    python tests/test_routes_smoke.py

Needs httpx on top of the app's own dependencies (it backs
fastapi.testclient.TestClient) — see requirements-dev.txt.

Three things this exists to catch:

  1. ROUTE ORDER. /console/oee is a literal path and /console/{zone} is a
     pattern. If console.router is registered first, FastAPI matches the OEE
     form as a zone named "oee" and 404s it. The failure is silent and looks
     exactly like a missing page.
  2. THE REVERT. /console/<zone> went back to good units only when OEE capture
     moved to end-of-shift. If scrap or downtime fields reappear there, the
     2-hour rounds have picked the extra work back up.
  3. WRITE ISOLATION on /console/oee — nothing is written for a machine whose
     values match what's already stored, so opening and saving the page does
     not append 34 identical rows.
  4. SHIFT LENGTH — the 8/10/12h control is offered on the 1st and 2nd Shift
     pages (each for its own shift), a later shift can't be shorter than the
     one before it, a 10/12h 2nd Shift is unticked by default and removes the
     SAME date's 3rd Shift row, and the downtime cap follows the machine's
     own length.
  5. SHORT DAYS — the typed 1-6 hours box on every row, the rule that the box
     only counts next to the short-day choice, the 3rd Shift page offering a
     length only after a short 2nd Shift, and /oee's 7/30-day Pareto.
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

from fastapi.testclient import TestClient  # noqa: E402

import app.db.entries as entries_mod  # noqa: E402
import app.db.oee as oee_mod  # noqa: E402
import app.routers.console as console_mod  # noqa: E402
import app.routers.console_oee as console_oee_mod  # noqa: E402
import app.routers.oee as oee_router  # noqa: E402
import app.routers.supervisor as sup_mod  # noqa: E402
from app.models import MACHINE_IDS, SHIFT_SLOTS  # noqa: E402

DAY = date(2026, 9, 8)
SHIFT = "1st Shift"
S1 = SHIFT_SLOTS[SHIFT]

# The floor's fourteen active codes, all unplanned: file 12's eleven with
# Operator Adjustments split four ways by docs/sql/13_split_operator_adjustments.sql.
REASONS = {
    code: {"code": code, "label": label, "is_planned": False}
    for code, label in [
        ("ROLL_CHANGE", "Roll Change"), ("SETUP", "Setup"), ("FAAR", "FAAR"),
        ("EQUIP_FAIL", "Equipment Failure"),
        ("OPER_ADJ_TEMP", "Operator Adjustments - Temperature"),
        ("OPER_ADJ_TIMING", "Operator Adjustments - Timing"),
        ("OPER_ADJ_PRESSURE", "Operator Adjustments - Pressure"),
        ("OPER_ADJ_AIRJET", "Operator Adjustments - Air jet"),
        ("DEFECT_MAT", "Defective Material"), ("LACK_MAT", "Lack of Material"),
        ("LACK_OPER", "Lack of Operator"), ("DELIVERY", "Delivery"),
        ("REGISTRATION", "Registration"), ("SHIFT_START", "Start of Shift"),
    ]
}
# Retired by file 13. Still in the table so history labels, never offered on
# an untouched machine, but must carry forward on a shift that already has it.
RETIRED = {
    "OPER_ADJUST": {"code": "OPER_ADJUST", "label": "Operator Adjustments",
                    "is_planned": False},
}
ALL_REASONS = {**REASONS, **RETIRED}

INCREMENTS = {m: 11700 for m in MACHINE_IDS}
entries = [
    {"machine_id": "C1", "operator": "Sam", "time_slot": slot,
     "units_produced": value, "status": ":)", "issue": None, "entered_by": "1234",
     "entry_date": DAY, "created_at": datetime(2026, 9, 8, 14, 0)}
    for slot, value in zip(S1, [11700, 23400, 35100, 46800])
]

# --- stub every read -------------------------------------------------------
entries_mod.get_latest_entries_for_date = lambda d: entries
entries_mod.get_available_entry_dates = lambda: [DAY]
entries_mod.get_available_production_days = lambda: [DAY]
entries_mod.get_production_day_entries = lambda d: entries
entries_mod.get_shift_activity = lambda d, slots: {"C1": {"operator": "Sam", "issues": []}}
console_mod.get_shift_activity = entries_mod.get_shift_activity
sup_mod.get_available_production_days = entries_mod.get_available_production_days
sup_mod.get_production_day_entries = entries_mod.get_production_day_entries
sup_mod.get_shift_activity = entries_mod.get_shift_activity
oee_router.get_available_production_days = entries_mod.get_available_production_days

stored_scrap = {}
stored_downtime = {}
stored_schedule = {}
stored_lengths = {}   # (machine_id, shift) -> hours, all for DAY

for mod in (oee_mod, console_oee_mod):
    mod.get_latest_scrap_for_date = lambda d: dict(stored_scrap)
    mod.get_latest_downtime_for_date = lambda d: dict(stored_downtime)
    mod.get_schedule_for_date = lambda d: dict(stored_schedule)
    mod.get_shift_lengths = lambda d: dict(stored_lengths) if d == DAY else {}
    mod.get_downtime_reasons = (
        lambda active_only=True: dict(REASONS if active_only else ALL_REASONS)
    )
# The range readers behind the 7/30-day Pareto, over the same stored state
# (all of it filed under DAY).
oee_mod.get_shift_lengths_for_range = lambda s, e: (
    {(m, DAY, sh): h for (m, sh), h in stored_lengths.items()} if s <= DAY <= e else {}
)
oee_mod.get_downtime_for_range = lambda s, e: (
    {(m, DAY, sh): v for (m, sh), v in stored_downtime.items()} if s <= DAY <= e else {}
)
oee_mod.get_schedule_for_range = lambda s, e: (
    {(m, DAY, sh): v for (m, sh), v in stored_schedule.items()} if s <= DAY <= e else {}
)
entries_mod.get_reported_slots = lambda s, e: {
    (r["machine_id"], r["entry_date"], r["time_slot"]) for r in entries
    if s <= r["entry_date"] <= e
}
oee_mod.get_ideal_rates = lambda: {m: i / 1.5 for m, i in INCREMENTS.items()}
oee_mod.get_shift_standards = lambda: {
    (m, s): i * 4 for m, i in INCREMENTS.items() for s in SHIFT_SLOTS
}

# --- capture every write ---------------------------------------------------
written = {"entry": [], "scrap": [], "downtime": [], "schedule": [], "length": []}
console_mod.create_entry = lambda p: written["entry"].append(p) or 1
console_oee_mod.create_shift_scrap = lambda p: written["scrap"].append(p) or 1
console_oee_mod.create_schedule_exception = lambda p: written["schedule"].append(p) or 1
console_oee_mod.create_shift_length = lambda p: written["length"].append(p) or 1


def fake_downtime(payload, *, shift_minutes=480):
    total = sum(r.minutes for r in payload.reasons)
    if total > shift_minutes:
        raise oee_mod.DowntimeExceedsShiftError(
            f"{total} minutes of downtime doesn't fit in a {shift_minutes}-minute shift "
            f"({payload.machine_id}, {payload.shift})."
        )
    unknown = [r.reason_code for r in payload.reasons if r.reason_code not in ALL_REASONS]
    if unknown:
        raise oee_mod.UnknownReasonCodeError(f"Unknown code(s): {unknown}")
    written["downtime"].append(payload)
    return 1


console_oee_mod.create_shift_downtime = fake_downtime

from app.main import app  # noqa: E402

client = TestClient(app)
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def radio_tag(html, machine, value):
    """The <input> tag of one option in a machine's length control."""
    start = html.index(f'name="hours_{machine}" value="{value}"')
    return html[start:html.index(">", start)]


def radio_disabled(html, machine, value):
    return "disabled" in radio_tag(html, machine, value)


print("\n== every page renders ==")
for path in ("/dashboard", "/dashboard/b3", "/supervisor", "/oee",
             "/console", "/console/b3", "/console/oee", "/healthz"):
    res = client.get(path)
    check(f"GET {path} -> 200", res.status_code == 200,
          f"got {res.status_code}: {res.text[:300]}")

print("\n== ROUTE ORDER: /console/oee is not eaten by /console/{zone} ==")
res = client.get("/console/oee")
check("not a 404", res.status_code == 200, str(res.status_code))
check("renders the OEE form, not a zone grid", "End of Shift OEE" in res.text)
check("unknown zones still 404", client.get("/console/nope").status_code == 404)

print("\n== THE REVERT: /console/<zone> is good units only ==")
batch = client.get("/console/b3").text
check("no scrap field", 'name="scrap_' not in batch)
check("no downtime fields", "dt_none_" not in batch and "dt_on_" not in batch)
check("no scheduled checkbox", 'name="scheduled_' not in batch)
# ...and still has exactly what the single-machine form has.
single = client.get("/console").text
for field in ("operator", "units_produced" if False else "units", "issue"):
    check(f"still has {field}", f'name="{field}_C1"' in batch or f'name="{field}"' in single)
check("time slot picker intact", 'name="time_slot"' in batch)
check("employee number intact", 'name="entered_by"' in batch)

print("\n== /console/oee form contents ==")
form = client.get("/console/oee").text
check("all 34 machines", form.count('name="sched_present_') == 34,
      str(form.count('name="sched_present_')))
check("all 14 reasons on C1", sum(
    1 for code in REASONS if f'name="dt_on_C1_{code}"' in form) == 14)
check("each reason has a minutes box",
      form.count('name="dt_min_C1_') == 14, str(form.count('name="dt_min_C1_')))
check("reason labels rendered", "Lack of Operator" in form and "FAAR" in form)
check("the four adjustment sub-reasons are offered",
      all(f'name="dt_on_C1_{c}"' in form for c in
          ("OPER_ADJ_TEMP", "OPER_ADJ_TIMING", "OPER_ADJ_PRESSURE", "OPER_ADJ_AIRJET")))
check("the retired umbrella code is NOT offered on an untouched machine",
      'name="dt_on_C1_OPER_ADJUST"' not in form)
check("no downtime checkbox", 'name="dt_none_C1"' in form)
check("scrap box", 'name="scrap_C1"' in form)
check("note box", 'name="note_C1"' in form)
check("scheduled ticked by default", 'name="scheduled_C1" value="1"\n                                   checked' in form
      or ('name="scheduled_C1"' in form and "checked" in form))


def post(fields, machines=None):
    """Posts the OEE form the way a browser would, for the given machines."""
    body = {"entered_by": "9001", "shift": SHIFT, "entry_date": DAY.isoformat()}
    for machine in (machines if machines is not None else MACHINE_IDS):
        body[f"sched_present_{machine}"] = "1"
        body[f"scheduled_{machine}"] = "1"
    body.update(fields)
    return client.post("/console/oee", data=body)


def reset():
    for bucket in written.values():
        bucket.clear()
    stored_scrap.clear()
    stored_downtime.clear()
    stored_schedule.clear()
    stored_lengths.clear()


print("\n== the flow the floor asked for: tick several reasons, minutes on each ==")
reset()
res = post({
    "dt_on_C1_EQUIP_FAIL": "1", "dt_min_C1_EQUIP_FAIL": "45",
    "dt_on_C1_DELIVERY": "1", "dt_min_C1_DELIVERY": "20",
    "dt_on_C1_REGISTRATION": "1", "dt_min_C1_REGISTRATION": "15",
    "scrap_C1": "320",
})
check("200", res.status_code == 200, str(res.status_code))
check("one downtime submission", len(written["downtime"]) == 1)
codes = sorted(r.reason_code for r in written["downtime"][0].reasons) if written["downtime"] else []
check("all three reasons carried",
      codes == ["DELIVERY", "EQUIP_FAIL", "REGISTRATION"], str(codes))
mins = {r.reason_code: r.minutes for r in written["downtime"][0].reasons}
check("with their own minutes", mins == {"EQUIP_FAIL": 45, "DELIVERY": 20, "REGISTRATION": 15},
      str(mins))
check("scrap saved as a shift total", len(written["scrap"]) == 1
      and written["scrap"][0].scrap_units == 320)
check("scrap is keyed by shift, not slot", written["scrap"][0].shift == SHIFT)

print("\n== 'no downtime' records a clean shift, not an absent one ==")
reset()
post({"dt_none_C2": "1"})
check("one submission", len(written["downtime"]) == 1)
check("with zero reasons", written["downtime"] and written["downtime"][0].reasons == [])

print("\n== contradictions and half-filled rows are refused ==")
reset()
res = post({"dt_none_C3": "1", "dt_on_C3_SETUP": "1", "dt_min_C3_SETUP": "30"})
check("none + a reason writes nothing", written["downtime"] == [])
check("and says so", "cannot be both" in res.text)

reset()
res = post({"dt_on_C4_SETUP": "1"})
check("ticked with no minutes writes nothing", written["downtime"] == [])
check("and names the reason", "Setup" in res.text and "no minutes" in res.text)

reset()
res = post({"dt_min_C5_SETUP": "30"})
check("minutes without the tick writes nothing", written["downtime"] == [])
check("and says the box isn't ticked", "isn" in res.text and "ticked" in res.text)

reset()
res = post({
    "dt_on_C6_SETUP": "1", "dt_min_C6_SETUP": "300",
    "dt_on_C6_DELIVERY": "1", "dt_min_C6_DELIVERY": "300",
})
check("over a full shift writes nothing", written["downtime"] == [])
check("and names the overflow", "600 minutes" in res.text, res.text[:200])

print("\n== WRITE ISOLATION: unchanged machines are not rewritten ==")
# Simulate a shift already entered, then open and save the page untouched.
stored_downtime[("C1", SHIFT)] = {
    "note": None, "planned_minutes": 0, "unplanned_minutes": 45,
    "reasons": [{"code": "EQUIP_FAIL", "label": "Equipment Failure",
                 "minutes": 45, "is_planned": False}],
}
stored_scrap[("C1", SHIFT)] = 320
for bucket in written.values():
    bucket.clear()

res = post({"dt_on_C1_EQUIP_FAIL": "1", "dt_min_C1_EQUIP_FAIL": "45", "scrap_C1": "320"})
check("identical values write NOTHING", written["downtime"] == [] and written["scrap"] == [],
      f"downtime={written['downtime']} scrap={written['scrap']}")
check("and the page says nothing changed", "Nothing changed" in res.text)

# ...but a real edit still saves.
for bucket in written.values():
    bucket.clear()
post({"dt_on_C1_EQUIP_FAIL": "1", "dt_min_C1_EQUIP_FAIL": "60", "scrap_C1": "320"})
check("changing the minutes does save", len(written["downtime"]) == 1)
check("scrap still untouched", written["scrap"] == [])

print("\n== RETIRED CODES CARRY FORWARD on shifts entered before the retirement ==")
# The trap this guards: a shift entered with Operator Adjustments before file
# 13 split it. The form only lists active codes, and the newest downtime
# header wins wholesale — so if the retired code were simply left off the
# page, re-saving this shift for ANY reason would silently drop its 30 minutes.
reset()
stored_downtime[("C1", SHIFT)] = {
    "note": None, "planned_minutes": 0, "unplanned_minutes": 45,
    "reasons": [{"code": "SETUP", "label": "Setup", "minutes": 15, "is_planned": False},
                {"code": "OPER_ADJUST", "label": "Operator Adjustments",
                 "minutes": 30, "is_planned": False}],
}
# Pin the shift: an unqualified GET follows the clock, and the fixture is 1st.
oee_url = f"/console/oee?shift={SHIFT}&entry_date={DAY.isoformat()}"
form = client.get(oee_url).text
check("retired code rendered on the machine that has it",
      'name="dt_on_C1_OPER_ADJUST"' in form)
check("ticked, with its minutes",
      'name="dt_min_C1_OPER_ADJUST"' in form and 'value="30"' in form)
check("tagged as retired", "dt-retired-tag" in form)
check("still NOT rendered on a machine that doesn't have it",
      'name="dt_on_C2_OPER_ADJUST"' not in form)
check("still 14 offered on that other machine",
      form.count('name="dt_min_C2_') == 14, str(form.count('name="dt_min_C2_')))

# Re-save exactly as the browser would post the pre-filled page, but with
# Setup corrected — the retired code's fields come back with it.
res = post({
    "dt_on_C1_SETUP": "1", "dt_min_C1_SETUP": "20",
    "dt_on_C1_OPER_ADJUST": "1", "dt_min_C1_OPER_ADJUST": "30",
})
check("200", res.status_code == 200, str(res.status_code))
check("one submission", len(written["downtime"]) == 1)
mins = {r.reason_code: r.minutes for r in written["downtime"][0].reasons} if written["downtime"] else {}
check("the retired code's minutes are on the new header too",
      mins == {"SETUP": 20, "OPER_ADJUST": 30}, str(mins))
check("and the re-rendered page still shows the retired row",
      'name="dt_on_C1_OPER_ADJUST"' in res.text)

# Untouched, nothing is written — the retired code round-trips as "unchanged".
for bucket in written.values():
    bucket.clear()
res = post({
    "dt_on_C1_SETUP": "1", "dt_min_C1_SETUP": "15",
    "dt_on_C1_OPER_ADJUST": "1", "dt_min_C1_OPER_ADJUST": "30",
})
check("saving it untouched writes nothing", written["downtime"] == [],
      str(written["downtime"]))

# Deliberately unticking it IS a reclassification, and drops it.
for bucket in written.values():
    bucket.clear()
res = post({
    "dt_on_C1_SETUP": "1", "dt_min_C1_SETUP": "15",
    "dt_min_C1_OPER_ADJUST": "",
    "dt_on_C1_OPER_ADJ_TEMP": "1", "dt_min_C1_OPER_ADJ_TEMP": "30",
})
mins = {r.reason_code: r.minutes for r in written["downtime"][0].reasons} if written["downtime"] else {}
check("unticking the retired code and picking a sub-reason reclassifies it",
      mins == {"SETUP": 15, "OPER_ADJ_TEMP": 30}, str(mins))
# The re-render still shows the row (its minutes box came back in the POST),
# now unticked — so a failed untick has something to fix. A fresh GET against
# the corrected record no longer has the code and drops the row.
after = res.text.split('name="dt_on_C1_OPER_ADJUST"')
check("re-rendered unticked", len(after) == 2 and "checked" not in after[1][:60])
stored_downtime[("C1", SHIFT)] = {
    "note": None, "planned_minutes": 0, "unplanned_minutes": 45,
    "reasons": [{"code": "SETUP", "label": "Setup", "minutes": 15, "is_planned": False},
                {"code": "OPER_ADJ_TEMP", "label": "Operator Adjustments - Temperature",
                 "minutes": 30, "is_planned": False}],
}
check("gone on the next open", 'name="dt_on_C1_OPER_ADJUST"' not in client.get(oee_url).text)

print("\n== the scheduled checkbox ==")
reset()
post({})
check("all ticked, nothing on record -> no write", written["schedule"] == [],
      str(written["schedule"]))

reset()
body = {"entered_by": "9001", "shift": SHIFT, "entry_date": DAY.isoformat()}
for machine in MACHINE_IDS:
    body[f"sched_present_{machine}"] = "1"
    if machine != "C5":
        body[f"scheduled_{machine}"] = "1"
client.post("/console/oee", data=body)
check("unticking one writes exactly one exception", len(written["schedule"]) == 1,
      str(written["schedule"]))
check("for that machine, scheduled=False",
      written["schedule"] and written["schedule"][0].machine_id == "C5"
      and written["schedule"][0].scheduled is False)

# THE REGRESSION THIS GUARDS: a POST that omits the scheduling controls
# entirely must leave scheduling alone. Without the hidden presence marker,
# every machine would read as "unticked" and be marked not-scheduled, silently
# removing the whole floor from the OEE denominator.
reset()
client.post("/console/oee", data={
    "entered_by": "9001", "shift": SHIFT, "entry_date": DAY.isoformat(),
    "dt_none_C1": "1",
})
check("a POST with no scheduling controls writes NO scheduling",
      written["schedule"] == [], str(written["schedule"]))

print("\n== shared-field validation ==")
res = client.post("/console/oee", data={"entered_by": "", "shift": SHIFT,
                                        "entry_date": DAY.isoformat()})
check("missing employee number blocked", "employee number" in res.text)
res = client.post("/console/oee", data={"entered_by": "9001", "shift": SHIFT,
                                        "entry_date": "not-a-date"})
check("bad date blocked", "valid date" in res.text)

print("\n== SHIFT LENGTH: each shift sets its own, on its own page ==")
reset()
first = client.get(f"/console/oee?shift=1st%20Shift&entry_date={DAY.isoformat()}").text
check("1st Shift page offers the radio group",
      all(f'name="hours_C1" value="{h}"' in first for h in (8, 10, 12)))
check("8h is the default", "checked" in first.split('name="hours_C1" value="8"')[1][:80])
check("every machine gets one (8h, 10h, 12h and the short day)",
      first.count('name="hours_') == 34 * 4, str(first.count('name="hours_')))
second = client.get(f"/console/oee?shift=2nd%20Shift&entry_date={DAY.isoformat()}").text
check("2nd Shift page offers it too", 'name="hours_C1" value="12"' in second)
check("...marked as following the 1st Shift when nothing is set", "follows 1st" in second)
third = client.get(f"/console/oee?shift=3rd%20Shift&entry_date={DAY.isoformat()}").text
check("3rd Shift page has NO radio group after an 8h 2nd (it can only be 8h)",
      'name="hours_C1"' not in third)
check("but shows it read-only", 'class="hours-readonly' in third and "10PM-6AM" in third)
check("the span line says the 3rd Shift lands the next morning",
      "10PM Tue 9/8 to 6AM Wed 9/9" in third, third[:0])

# Picking 12h for C1 on the 1st Shift page writes one length row for that shift.
res = post({"hours_C1": "12"})
check("200", res.status_code == 200, str(res.status_code))
check("one length row written", len(written["length"]) == 1, str(written["length"]))
check("for C1, 1st Shift, 12 hours, on the date in the box",
      written["length"] and written["length"][0].machine_id == "C1"
      and written["length"][0].shift == "1st Shift"
      and written["length"][0].shift_hours == 12
      and written["length"][0].entry_date == DAY)
check("saved badge names it", "12h shifts" in res.text)

# The manager's case: an ordinary 1st Shift, then a 12-hour crew at 6PM. Set
# on the 2nd Shift page, for the 2nd Shift.
reset()
res = client.post("/console/oee", data={"entered_by": "9001", "shift": "2nd Shift",
                                        "entry_date": DAY.isoformat(), "hours_C1": "12",
                                        "sched_present_C1": "1", "scheduled_C1": "1"})
check("a 12h 2nd Shift after an 8h 1st is accepted", res.status_code == 200, res.text[:300])
check("written for the 2nd Shift", len(written["length"]) == 1
      and written["length"][0].shift == "2nd Shift" and written["length"][0].shift_hours == 12,
      str(written["length"]))
check("and ticking Scheduled with it writes the exception (default is off)",
      len(written["schedule"]) == 1 and written["schedule"][0].scheduled is True,
      str(written["schedule"]))

reset()
post({"hours_C1": "8"})
check("posting 8h over nothing stored writes nothing", written["length"] == [])
reset()
client.post("/console/oee", data={"entered_by": "9001", "shift": "3rd Shift",
                                  "entry_date": DAY.isoformat(), "hours_C1": "12"})
check("a length posted from the 3rd Shift page is ignored", written["length"] == [],
      str(written["length"]))
reset()
res = post({"hours_C1": "9"})
check("an unknown length is refused", written["length"] == [] and "8, 10 or 12" in res.text)

print("\n== SHIFT LENGTH: as long or longer, never shorter ==")
reset()
stored_lengths[("C1", "1st Shift")] = 12
second = client.get(f"/console/oee?shift=2nd%20Shift&entry_date={DAY.isoformat()}").text
c1_hours = second.split('aria-label="Shift length for C1"')[1].split("</div>")[0]
check("on the 2nd Shift page, 8h, 10h and the short day are disabled after a 12h 1st",
      [radio_disabled(second, "C1", v) for v in ("8", "10", "12", "short")]
      == [True, True, False, True], c1_hours[:400])
check("...and say why", "would start inside it" in c1_hours)
res = client.post("/console/oee", data={"entered_by": "9001", "shift": "2nd Shift",
                                        "entry_date": DAY.isoformat(), "hours_C1": "8"})
check("posting a shorter 2nd Shift anyway is refused", written["length"] == [])
check("...naming both shifts", "2nd Shift" in res.text and "shorter than 1st Shift" in res.text)

reset()
stored_lengths[("C1", "2nd Shift")] = 8
first = client.get(f"/console/oee?shift=1st%20Shift&entry_date={DAY.isoformat()}").text
c1_hours = first.split('aria-label="Shift length for C1"')[1].split("</div>")[0]
check("on the 1st Shift page, 10h and 12h are disabled when the 2nd is set to 8h",
      c1_hours.count("disabled") == 2 and "Change that first" in c1_hours, c1_hours[:400])
res = post({"hours_C1": "12"})
check("posting a longer 1st Shift anyway is refused", written["length"] == [],
      str(written["length"]))
# ...but an inherited 2nd Shift follows the 1st, so raising the 1st is fine.
reset()
post({"hours_C1": "12"})
check("raising the 1st with nothing set on the 2nd is fine", len(written["length"]) == 1)

print("\n== SHIFT LENGTH: what a 12-hour 2nd Shift does to the day ==")
reset()
stored_lengths[("C1", "1st Shift")] = 12
second = client.get(f"/console/oee?shift=2nd%20Shift&entry_date={DAY.isoformat()}").text
c1_sched = second.split('name="scheduled_C1"')[1][:200]
check("2nd Shift's Scheduled box starts UNTICKED on a 12h night", "checked" not in c1_sched)
check("and says why", "night crew" in c1_sched)
check("with the night span", "6PM-6AM" in second)
c2_sched = second.split('name="scheduled_C2"')[1][:80]
check("an 8h machine's box is still ticked", "checked" in c2_sched)

body = {"entered_by": "9001", "shift": "2nd Shift", "entry_date": DAY.isoformat(),
        "sched_present_C1": "1", "scheduled_C1": "1"}
client.post("/console/oee", data=body)
check("ticking the night crew on writes scheduled=True",
      len(written["schedule"]) == 1 and written["schedule"][0].scheduled is True
      and written["schedule"][0].shift == "2nd Shift", str(written["schedule"]))

# The SAME date's 3rd Shift row is the one a long 2nd Shift removes — the
# date is the day the shift started.
third = client.get(f"/console/oee?shift=3rd%20Shift&entry_date={DAY.isoformat()}").text
check("3rd Shift of the same date shows 'no 3rd shift' for C1",
      "No 3rd shift" in third and "2nd Shift ran 12h (6PM-6AM)" in third, third[:0])
check("with no inputs for it", 'name="scrap_C1"' not in third
      and 'name="sched_present_C1"' not in third and 'name="dt_none_C1"' not in third)
check("while C2 is untouched", 'name="scrap_C2"' in third)
next_day = DAY + timedelta(days=1)
third_next = client.get(f"/console/oee?shift=3rd%20Shift&entry_date={next_day.isoformat()}").text
check("the NEXT date's 3rd Shift is unaffected",
      'name="scrap_C1"' in third_next and "No 3rd shift" not in third_next)

# Posting the 3rd Shift form doesn't touch C1 even if a stale browser sends
# fields for it.
reset()
stored_lengths[("C1", "1st Shift")] = 12
client.post("/console/oee", data={"entered_by": "9001", "shift": "3rd Shift",
                                  "entry_date": DAY.isoformat(),
                                  "dt_none_C1": "1", "scrap_C1": "5",
                                  "sched_present_C1": "1"})
check("a shift that doesn't exist is never written",
      not written["downtime"] and not written["scrap"] and not written["schedule"],
      str(written))

print("\n== SHIFT LENGTH: the downtime cap follows the machine ==")
reset()
stored_lengths[("C1", "1st Shift")] = 12
res = post({"dt_on_C1_SETUP": "1", "dt_min_C1_SETUP": "300",
            "dt_on_C1_DELIVERY": "1", "dt_min_C1_DELIVERY": "300"})
check("600 minutes fits a 12h shift", len(written["downtime"]) == 1, res.text[:300])
check("minutes box max is 720 for that machine",
      'max="720"' in client.get(f"/console/oee?shift=1st%20Shift&entry_date={DAY.isoformat()}").text)
reset()
res = post({"dt_on_C2_SETUP": "1", "dt_min_C2_SETUP": "300",
            "dt_on_C2_DELIVERY": "1", "dt_min_C2_DELIVERY": "300"})
check("...but not an 8h one", written["downtime"] == [] and "480-minute" in res.text)
# Changing the length and entering the minutes in the same save: the new
# length is the cap.
reset()
res = post({"hours_C1": "12",
            "dt_on_C1_SETUP": "1", "dt_min_C1_SETUP": "300",
            "dt_on_C1_DELIVERY": "1", "dt_min_C1_DELIVERY": "300"})
check("length changed on the same save is the cap",
      len(written["length"]) == 1 and len(written["downtime"]) == 1, res.text[:300])
reset()

print("\n== SHORT DAYS: typed hours, 1 to 6 ==")
FIRST_URL = f"/console/oee?shift=1st%20Shift&entry_date={DAY.isoformat()}"
SECOND_URL = f"/console/oee?shift=2nd%20Shift&entry_date={DAY.isoformat()}"
THIRD_URL = f"/console/oee?shift=3rd%20Shift&entry_date={DAY.isoformat()}"


def short_box(html, machine):
    """The short-day hours <input> tag for one machine."""
    start = html.index(f'name="short_hours_{machine}"')
    return html[start:html.index(">", start)]


def post_shift(shift, fields):
    body = {"entered_by": "9001", "shift": shift, "entry_date": DAY.isoformat()}
    body.update(fields)
    return client.post("/console/oee", data=body)


reset()
first = client.get(FIRST_URL).text
check("every row has the short-day option", first.count('value="short"') == 34,
      str(first.count('value="short"')))
check("...with its hours box, 1 to 6", 'min="1" max="6"' in short_box(first, "C1"))
check("the short day is open on the 1st Shift", not radio_disabled(first, "C1", "short"))
check("the hint says the hours are SCHEDULED hours", "<em>scheduled</em>" in first
      and "Lack of Operator" in first)
check("the set-every-machine control is on the page", 'id="set-all-hours"' in first)

res = post({"hours_C1": "short", "short_hours_C1": "6"})
check("6 hours saves one length row of 6",
      len(written["length"]) == 1 and written["length"][0].shift_hours == 6, str(written["length"]))
check("badge says short day", "6h short day" in res.text)

reset()
res = post({"hours_C1": "short", "short_hours_C1": ""})
check("short day with no hours writes nothing, and says so",
      written["length"] == [] and "no hours are typed" in res.text)
reset()
res = post({"hours_C1": "short", "short_hours_C1": "7"})
check("7 is refused", written["length"] == [] and "1 to 6" in res.text)
reset()
res = post({"hours_C1": "short", "short_hours_C1": "2.5"})
check("a fraction is refused", written["length"] == [] and "whole number" in res.text)
reset()
res = post({"hours_C1": "8", "short_hours_C1": "4"})
check("hours typed next to 8h are a contradiction, not a guess",
      written["length"] == [] and "pick the short day, or clear the box" in res.text)

print("\n== SHORT DAYS: a stored 4-hour morning ==")
reset()
stored_lengths[("C1", "1st Shift")] = 4
first = client.get(FIRST_URL).text
check("the box is pre-filled with 4", 'value="4"' in short_box(first, "C1"))
check("...with the short day picked", "checked" in radio_tag(first, "C1", "short"))
check("...and the span under it", "6AM-10AM" in first)
check("the minutes boxes are capped at 240", 'max="240"' in first)
post({"hours_C1": "short", "short_hours_C1": "4"})
check("saving it untouched writes nothing", written["length"] == [], str(written["length"]))
res = post({"hours_C1": "short", "short_hours_C1": "4",
            "dt_on_C1_SETUP": "1", "dt_min_C1_SETUP": "300"})
check("300 minutes of downtime don't fit 4 hours",
      written["downtime"] == [] and "240-minute" in res.text, res.text[:300])

second = client.get(SECOND_URL).text
check("the 2nd Shift follows the pattern: a full 6-hour short day",
      "checked" in radio_tag(second, "C1", "short") and 'value="6"' in short_box(second, "C1"))
check("8h/10h/12h are all still possible after a short morning",
      not any(radio_disabled(second, "C1", v) for v in ("8", "10", "12")))
c1_sched = second.split('name="scheduled_C1"')[1][:400]
check("its Scheduled box starts unticked", "checked" not in c1_sched.split(">")[0])
check("...and says why", "short day" in c1_sched)
check("an 8h machine's 2nd Shift is untouched", "checked" in second.split('name="scheduled_C2"')[1][:80])

reset()
stored_lengths[("C1", "1st Shift")] = 4
post_shift("2nd Shift", {"hours_C1": "short", "short_hours_C1": "6",
                         "sched_present_C1": "1", "scheduled_C1": "1"})
check("ticking the afternoon crew on writes scheduled=True and no length",
      len(written["schedule"]) == 1 and written["schedule"][0].scheduled is True
      and written["length"] == [], f"{written['schedule']} {written['length']}")

print("\n== SHORT DAYS: the 3rd Shift page ==")
third = client.get(THIRD_URL).text
check("offers a length after a short 2nd Shift: 8h or a short day",
      'name="hours_C1" value="short"' in third and 'name="hours_C1" value="8"' in third)
check("...never 10h or 12h (they'd run past 6AM)",
      'name="hours_C1" value="10"' not in third and 'name="hours_C1" value="12"' not in third)
check("the short day is picked, 6PM-12AM", "checked" in radio_tag(third, "C1", "short")
      and "6PM-12AM" in third)
check("an ordinary machine's 3rd Shift row is still read-only", 'name="hours_C2"' not in third)
check("inherited rows say they follow the 2nd", "follows 2nd" in third)

reset()
stored_lengths[("C1", "1st Shift")] = 4
post_shift("3rd Shift", {"hours_C1": "short", "short_hours_C1": "5",
                         "sched_present_C1": "1", "scheduled_C1": "1"})
check("5 hours on the 3rd Shift saves as the 3rd Shift's",
      len(written["length"]) == 1 and written["length"][0].shift == "3rd Shift"
      and written["length"][0].shift_hours == 5, str(written["length"]))
check("with the evening crew ticked on",
      len(written["schedule"]) == 1 and written["schedule"][0].scheduled is True)

print("\n== SHORT DAYS: never inside a longer shift ==")
reset()
second = client.get(SECOND_URL).text
check("the short day is disabled on the 2nd Shift after an 8h 1st",
      radio_disabled(second, "C1", "short") and "disabled" in short_box(second, "C1"))
res = post_shift("2nd Shift", {"hours_C1": "short", "short_hours_C1": "4"})
check("posting it anyway is refused, saying why",
      written["length"] == [] and "short-day shift after 1st Shift" in res.text)
reset()
stored_lengths[("C1", "1st Shift")] = 4
stored_lengths[("C1", "2nd Shift")] = 6
first = client.get(FIRST_URL).text
check("with a short 2nd Shift set, the 1st can only be a short day",
      [radio_disabled(first, "C1", v) for v in ("8", "10", "12", "short")]
      == [True, True, True, False])
res = post({"hours_C1": "8"})
check("...and lengthening it anyway is refused",
      written["length"] == [] and "short-day shift after 1st Shift" in res.text)
reset()

print("\n== THE PRODUCTION DAY: the 3rd Shift page defaults to the day it started ==")
from app.models import default_production_day  # noqa: E402
expected = default_production_day("3rd Shift", datetime.now())
third_default = client.get("/console/oee?shift=3rd%20Shift").text
check("3rd Shift page's date box defaults to the production day",
      f'value="{expected.isoformat()}"' in third_default, expected.isoformat())

print("\n== /oee is shift-grain now ==")
oee_page = client.get("/oee").text
check("no per-slot OEE columns", 'class="cell slot-cell"' not in oee_page)
check("has the shift summary columns", 'data-field="oee"' in oee_page
      and 'data-field="availability"' in oee_page)
check("has a reasons column", 'data-field="reasons"' in oee_page)
check("completeness says Production", 'data-field="production"' in oee_page)

data = client.get("/api/oee-data")
check("/api/oee-data 200", data.status_code == 200, data.text[:300])
payload = data.json()
check("all three shifts", set(payload["shifts"]) == {"1st Shift", "2nd Shift", "3rd Shift"})
check("the 3rd Shift's checkpoints carry the next calendar date",
      all(c["date"] == (DAY + timedelta(days=1)).isoformat()
          for c in payload["shifts"]["3rd Shift"]["machines"]["C1"]["checkpoints"]))
check("machines carry no per-slot OEE",
      "slots" not in payload["shifts"][SHIFT]["machines"]["C1"])
check("but do carry the checkpoint sequence",
      len(payload["shifts"][SHIFT]["machines"]["C1"]["checkpoints"]) == 4)
check("and their shift length", payload["shifts"][SHIFT]["machines"]["C1"]["shift_hours"] == 8
      and payload["shifts"][SHIFT]["machines"]["C1"]["shift_exists"] is True
      and payload["shifts"][SHIFT]["machines"]["C1"]["span"] == "6AM-2PM")
check("/oee page has the length tag slot", 'class="shift-length"' in oee_page)
check("/oee has the Pareto picker with a chip per zone",
      oee_page.count('class="quick-pick pareto-chip"') == 6)   # All + 5 zones
check("...and a checkbox per machine",
      oee_page.count('<input type="checkbox" value="') == 34)
check("?pareto= is passed through to the page",
      'window.INITIAL_PARETO = "C1,C2"' in client.get("/oee?pareto=C1,C2").text)

print("\n== the Pareto's 7 and 30-day periods ==")
check("/oee has the period chips",
      all(f'data-period="{p}"' in oee_page for p in ("shift", "7", "30")))
check("?period=30 is passed through", "window.INITIAL_PERIOD = 30" in client.get("/oee?period=30").text)
check("an unknown period means the shift",
      "window.INITIAL_PERIOD = null" in client.get("/oee?period=9").text)
reset()
stored_downtime[("C2", SHIFT)] = {
    "note": None, "planned_minutes": 0, "unplanned_minutes": 20,
    "reasons": [{"code": "SETUP", "label": "Setup", "minutes": 20, "is_planned": False}],
}
rng = client.get(f"/api/oee-pareto?end={DAY.isoformat()}&days=30")
check("/api/oee-pareto 200", rng.status_code == 200, rng.text[:300])
body = rng.json() if rng.status_code == 200 else {"machines": {}}
check("30 days ending on the date", body.get("days") == 30 and body.get("end") == DAY.isoformat())
check("every machine, so the picker can narrow it", set(body["machines"]) == set(MACHINE_IDS))
check("C2's reasons are there",
      [(r["code"], r["minutes"]) for r in body["machines"].get("C2", {}).get("reasons", [])]
      == [("SETUP", 20)])
check("C1 ran with no downtime entered: counted as missing",
      body["machines"].get("C1", {}).get("missing") == 1)
check("a junk period falls back to 7", client.get("/api/oee-pareto?days=abc").json()["days"] == 7)
check("no end means the latest day with data",
      client.get("/api/oee-pareto").json()["end"] == DAY.isoformat())
reset()

print("\n== /dashboard is the site menu ==")
index = client.get("/dashboard").text
for link in ("/console", "/console/oee", "/supervisor", "/oee",
             "/dashboard/b3", "/console/b3"):
    check(f"links to {link}", f'href="{link}"' in index)

print("\n== the [hidden] reset exists ==")
# Not a route check, but nothing else can catch this and it shipped a visible
# bug: `el.hidden = true` works only through the browser's own
# `[hidden] { display: none }`, which ANY author rule setting `display` beats.
# `.flag-banner` and `.completeness` are both toggled with .hidden AND styled
# display:flex, so /oee showed an empty red error banner on days with nothing
# wrong. A global reset is the fix; this asserts nobody removes it.
css = (Path(__file__).resolve().parents[1] / "app/static/css/style.css").read_text(encoding="utf-8")
normalised = " ".join(css.split())
check("style.css has [hidden] { display: none !important }",
      "[hidden] { display: none !important; }" in normalised)

# And flag every element that would silently break if it were removed, so the
# list stays visible to whoever reads this next.
import re  # noqa: E402
toggled = set()
for js in (Path(__file__).resolve().parents[1] / "app/static/js").glob("*.js"):
    for name in re.findall(r"(\w+)\.hidden\s*=", js.read_text(encoding="utf-8")):
        toggled.add(name)
check("elements are toggled via .hidden (so the reset matters)", len(toggled) > 0,
      str(sorted(toggled)))

print("\n== existing pages unaffected ==")
check("/api/dashboard-data 200", client.get("/api/dashboard-data").status_code == 200)
check("/api/supervisor-data 200", client.get("/api/supervisor-data").status_code == 200)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL SMOKE CHECKS PASSED")
