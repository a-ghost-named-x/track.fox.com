"""End-to-end smoke test: real routes, real Jinja rendering, stubbed database.

Run it from the REPO ROOT — Jinja2Templates resolves "app/templates"
relatively, so it won't find the templates from anywhere else:

    python tests/test_routes_smoke.py

Needs httpx on top of the app's own dependencies (it backs
fastapi.testclient.TestClient) — see requirements-dev.txt.

Renders every page and exercises the /console write paths against stubbed
database functions, capturing what WOULD have been written. The checks that
matter most are the write-isolation ones: three different people share the
batch form, and a blank field has to mean "I'm not touching this" rather than
"set this to nothing".

The regression it exists to prevent: an unticked checkbox and an absent one are
indistinguishable in a form POST, so reading absence as "unticked" marked every
machine not on the submitted form as NOT SCHEDULED — silently removing them
from the OEE denominator and inflating every number on the page. The hidden
sched_present_<machine> field is what tells the two apart.
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

from fastapi.testclient import TestClient

import app.db.entries as entries_mod
import app.db.oee as oee_mod
import app.routers.console as console_mod
import app.routers.oee as oee_router
import app.routers.supervisor as sup_mod
from app.models import SHIFT_SLOTS

DAY = date(2026, 9, 2)
S1 = SHIFT_SLOTS["1st Shift"]

REASONS = {
    "MATL": {"code": "MATL", "label": "Material wait or shortage", "is_planned": False},
    "MECH": {"code": "MECH", "label": "Mechanical failure", "is_planned": False},
    "CHGOVR": {"code": "CHGOVR", "label": "Changeover / setup", "is_planned": False},
    "PM": {"code": "PM", "label": "Planned maintenance", "is_planned": True},
}

INCREMENTS = {"C1": 11700, "C2": 11700, "C3": 11700, "C4": 11700, "C5": 11700,
              "C6": 10800, "C7": 10800, "C8": 10800, "C9": 10800, "C10": 10800,
              "C11": 10800, "C14": 11700, "C15": 11700, "C16": 11700,
              "FM1": 8100, "FM2": 8100, "FM3": 8100,
              "WS1": 9900, "WS2": 9900, "WS3": 9900, "WS4": 9900, "WS5": 9900,
              "WS6": 9900, "P1": 14400, "P2": 16200, "P3": 16200, "P4": 16200,
              "AS1": 2520, "AS2": 2520, "AS3": 2520, "AS4": 2520, "AS5": 2520,
              "AS6": 2250, "AS7": 2250}

entries = [
    {"machine_id": "C1", "operator": "Sam", "time_slot": slot,
     "units_produced": value, "status": ":)", "issue": None, "entered_by": "1234",
     "entry_date": DAY, "created_at": datetime(2026, 9, 2, 14, 0)}
    for slot, value in zip(S1, [11700, 23400, 35100, 46800])
]
downtime = {("C1", s): {"note": None, "reasons": [], "planned_minutes": 0,
                        "unplanned_minutes": 0} for s in S1}
scrap = {("C1", s): 0 for s in S1}
standards = {(m, s): inc * i
             for m, inc in INCREMENTS.items()
             for label, slots in SHIFT_SLOTS.items()
             for i, s in enumerate(slots, start=1)}
ideal = {m: inc / 1.5 for m, inc in INCREMENTS.items()}

# --- stub every read -------------------------------------------------------
for mod in (entries_mod, sup_mod, console_mod, oee_router):
    if hasattr(mod, "get_available_entry_dates"):
        mod.get_available_entry_dates = lambda: [DAY]
    if hasattr(mod, "get_latest_entries_for_date"):
        mod.get_latest_entries_for_date = lambda d: entries
    if hasattr(mod, "get_shift_activity"):
        mod.get_shift_activity = lambda d, slots: {"C1": {"operator": "Sam", "issues": []}}

entries_mod.get_latest_entries_for_date = lambda d: entries
entries_mod.get_available_entry_dates = lambda: [DAY]

for mod in (oee_mod, console_mod):
    mod.get_latest_scrap_for_date = lambda d: dict(scrap)
    mod.get_latest_downtime_for_date = lambda d: dict(downtime)
    mod.get_schedule_for_date = lambda d: {}
    mod.get_downtime_reasons = lambda active_only=True: dict(REASONS)
oee_mod.get_ideal_rates = lambda: ideal
oee_mod.get_slot_standards = lambda: standards

# --- capture every write ---------------------------------------------------
written = {"entry": [], "scrap": [], "downtime": [], "schedule": []}
console_mod.create_entry = lambda p: written["entry"].append(p) or 1
console_mod.create_scrap = lambda p: written["scrap"].append(p) or 1


def fake_create_downtime(payload):
    total = sum(r.minutes for r in payload.reasons)
    if total > 120:
        raise oee_mod.DowntimeExceedsSlotError(
            f"{total} minutes of downtime doesn't fit in a 120-minute slot "
            f"({payload.machine_id} {payload.time_slot})."
        )
    unknown = [r.reason_code for r in payload.reasons if r.reason_code not in REASONS]
    if unknown:
        raise oee_mod.UnknownReasonCodeError(f"Unknown code(s): {unknown}")
    written["downtime"].append(payload)
    return 1


console_mod.create_downtime = fake_create_downtime
console_mod.create_schedule_exception = lambda p: written["schedule"].append(p) or 1

from app.main import app  # noqa: E402

client = TestClient(app)
failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


print("\n== every page renders ==")
for path in ("/dashboard", "/dashboard/b3", "/supervisor", "/oee",
             "/console", "/console/b3", "/healthz"):
    res = client.get(path)
    check(f"GET {path} -> 200", res.status_code == 200,
          f"got {res.status_code}: {res.text[:400]}")

print("\n== 404s still 404 ==")
check("GET /dashboard/nope -> 404", client.get("/dashboard/nope").status_code == 404)
check("GET /console/nope -> 404", client.get("/console/nope").status_code == 404)

print("\n== /oee page content ==")
page = client.get("/oee").text
check("no explanatory 75% banner (removed by request)",
      "not 100%" not in page and "oee-anchor" not in page)
check("renders all 12 slot headers", all(f'data-slot="{s}"' in page for s in
      ["8AM", "10AM", "12PM", "2PM", "4PM", "6PM", "8PM", "10PM", "12AM", "2AM", "4AM", "6AM"]))
check("has no All Day option", "All Day" not in page)
check("renders all 34 machine rows", page.count("<tr data-machine=") == 34,
      f"count {page.count('<tr data-machine=')}")
check("loads oee.js", "/static/js/oee.js" in page)

print("\n== /api/oee-data ==")
res = client.get("/api/oee-data")
check("200", res.status_code == 200, res.text[:400])
data = res.json()
check("has all three shifts", set(data["shifts"]) == {"1st Shift", "2nd Shift", "3rd Shift"})
check("C1 scores 0.75", abs(data["shifts"]["1st Shift"]["machines"]["C1"]["shift"]["oee"] - 0.75) < 1e-9,
      str(data["shifts"]["1st Shift"]["machines"]["C1"]["shift"]["oee"]))
check("zones present", "b3" in data["shifts"]["1st Shift"]["zones"])
check("available_dates echoed", data["available_dates"] == ["2026-09-02"])
check("bad date param doesn't 500", client.get("/api/oee-data?date=garbage").status_code == 200)

print("\n== /console/b3 form has the new fields ==")
form = client.get("/console/b3").text
check("scrap input", 'name="scrap_C1"' in form)
check("downtime none checkbox", 'name="dt_none_C1"' in form)
check("two reason pairs", 'name="dt_reason1_C1"' in form and 'name="dt_reason2_C1"' in form)
check("reason options rendered", "Material wait or shortage" in form)
check("scheduled checkbox, ticked by default",
      'name="scheduled_C1"' in form and "checked" in form)
check("tick-all shortcut", 'id="mark-all-clean"' in form)


def post(fields):
    body = {"entered_by": "9001", "time_slot": "8AM", "shift": "1st Shift",
            "entry_date": DAY.isoformat()}
    body.update(fields)
    return client.post("/console/b3", data=body)


print("\n== scrap-only save must NOT touch units ==")
written["entry"].clear(); written["scrap"].clear()
res = post({"scrap_C1": "250"})
check("200", res.status_code == 200, str(res.status_code))
check("scrap written", len(written["scrap"]) == 1 and written["scrap"][0].scrap_cumulative == 250)
check("no entry written", written["entry"] == [])

print("\n== units-only save must NOT touch scrap ==")
written["entry"].clear(); written["scrap"].clear()
post({"units_C2": "11700", "operator_C2": "Dana"})
check("entry written", len(written["entry"]) == 1 and written["entry"][0].units_produced == 11700)
check("no scrap written", written["scrap"] == [])

print("\n== 'none' checkbox records a clean slot (header, zero reasons) ==")
written["downtime"].clear()
post({"dt_none_C3": "1"})
check("downtime written", len(written["downtime"]) == 1)
check("with zero reasons", written["downtime"] and written["downtime"][0].reasons == [])

print("\n== two reasons in one submission ==")
written["downtime"].clear()
post({"dt_reason1_C4": "MATL", "dt_min1_C4": "15",
      "dt_reason2_C4": "MECH", "dt_min2_C4": "10"})
check("one submission", len(written["downtime"]) == 1)
codes = [r.reason_code for r in written["downtime"][0].reasons] if written["downtime"] else []
check("both reasons carried", codes == ["MATL", "MECH"], str(codes))

print("\n== 'none' plus a reason is a contradiction, not a silent pick ==")
written["downtime"].clear()
res = post({"dt_none_C5": "1", "dt_reason1_C5": "MATL", "dt_min1_C5": "20"})
check("nothing written", written["downtime"] == [])
check("error shown to the user", "can" in res.text and "both" in res.text)

print("\n== downtime past the slot length is rejected with a readable message ==")
written["downtime"].clear()
res = post({"dt_reason1_C6": "MATL", "dt_min1_C6": "90",
            "dt_reason2_C6": "MECH", "dt_min2_C6": "60"})
check("nothing written", written["downtime"] == [])
check("message names the overflow", "150 minutes" in res.text, res.text[:200])

print("\n== minutes without a reason is caught ==")
written["downtime"].clear()
res = post({"dt_min1_C7": "20"})
check("nothing written", written["downtime"] == [])
check("asks for a reason", "reason" in res.text.lower())

print("\n== the scheduled checkbox ==")
# A browser posts the hidden sched_present_<m> for every machine on the form,
# and scheduled_<m> only for the ticked ones. These helpers reproduce that.
ZONE_B3 = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10", "C11",
           "C14", "C15", "C16"]


def post_full_form(fields, unticked=()):
    body = {}
    for machine in ZONE_B3:
        body[f"sched_present_{machine}"] = "1"
        if machine not in unticked:
            body[f"scheduled_{machine}"] = "1"
    body.update(fields)
    return post(body)


written["schedule"].clear()
post_full_form({"scrap_C1": "10"})
check("all ticked, nothing on record -> no write", written["schedule"] == [],
      str(written["schedule"]))

written["schedule"].clear()
post_full_form({}, unticked=["C5"])
check("unticking one machine writes exactly one exception",
      len(written["schedule"]) == 1, str(written["schedule"]))
check("and it is scheduled=False for that machine",
      written["schedule"] and written["schedule"][0].machine_id == "C5"
      and written["schedule"][0].scheduled is False, str(written["schedule"]))

# THE REGRESSION THIS GUARDS: a POST that omits the scheduling controls
# entirely (a hand-built request, or a form that never rendered them) must
# leave scheduling alone. Without the hidden presence marker, every machine
# would be read as "unticked" and marked not-scheduled — silently removing the
# whole zone from the OEE denominator.
written["schedule"].clear()
post({"units_C8": "10800", "operator_C8": "Lee"})
check("a POST with no scheduling controls writes NOTHING",
      written["schedule"] == [], str(written["schedule"]))

written["schedule"].clear()
console_mod.get_schedule_for_date = lambda d: {("C9", "1st Shift"): False}
post_full_form({})
check("re-ticking a not-scheduled machine writes scheduled=True",
      len(written["schedule"]) == 1
      and written["schedule"][0].machine_id == "C9"
      and written["schedule"][0].scheduled is True,
      str(written["schedule"]))
console_mod.get_schedule_for_date = lambda d: {}

print("\n== filling ONE machine writes nothing for the other 13 ==")
# The guarantee the whole shared-form design rests on. A zone form posts a
# field for every machine in the zone whether or not anyone typed in it, so
# each write path has to be gated on its OWN field being non-empty. If a blank
# box ever became a 0, one person entering one machine would zero the entire
# zone's production, scrap and availability in a single click.
for bucket in written.values():
    bucket.clear()
post_full_form({
    "units_C1": "11700", "operator_C1": "Sam",
    "scrap_C1": "40", "dt_none_C1": "1",
})
check("exactly one production entry, for C1",
      [p.machine_id for p in written["entry"]] == ["C1"],
      str([p.machine_id for p in written["entry"]]))
check("exactly one scrap row, for C1",
      [p.machine_id for p in written["scrap"]] == ["C1"],
      str([p.machine_id for p in written["scrap"]]))
check("exactly one downtime submission, for C1",
      [p.machine_id for p in written["downtime"]] == ["C1"],
      str([p.machine_id for p in written["downtime"]]))
check("no scheduling rows at all", written["schedule"] == [],
      str(written["schedule"]))
check("no zero-valued row written for any other machine",
      not [p for p in written["entry"] + written["scrap"] if p.machine_id != "C1"])

print("\n== a blank units box is not a zero ==")
# Belt and braces: an EXPLICIT zero is a real reading and must still save,
# so the gate has to be on the field being empty, not on it being falsy.
for bucket in written.values():
    bucket.clear()
post_full_form({"units_C2": "0", "operator_C2": "Dana", "scrap_C2": "0"})
check("an explicit 0 units still saves",
      [p.machine_id for p in written["entry"]] == ["C2"]
      and written["entry"][0].units_produced == 0,
      str(written["entry"]))
check("an explicit 0 scrap still saves",
      [p.machine_id for p in written["scrap"]] == ["C2"]
      and written["scrap"][0].scrap_cumulative == 0,
      str(written["scrap"]))

print("\n== scheduling alone needs no time slot ==")
written["schedule"].clear()
console_mod.get_schedule_for_date = lambda d: {}
res = client.post("/console/b3", data={
    "entered_by": "9001", "time_slot": "", "shift": "1st Shift",
    "entry_date": DAY.isoformat(),
})
check("no top error about time slot", "Pick a time slot" not in res.text)
console_mod.get_schedule_for_date = lambda d: {}

print("\n== slot data without a time slot IS blocked ==")
res = client.post("/console/b3", data={
    "entered_by": "9001", "time_slot": "", "shift": "1st Shift",
    "entry_date": DAY.isoformat(), "scrap_C1": "5",
})
check("blocked with a clear message", "Pick a time slot" in res.text)

print("\n== slot/shift mismatch still rejected ==")
res = client.post("/console/b3", data={
    "entered_by": "9001", "time_slot": "4PM", "shift": "1st Shift",
    "entry_date": DAY.isoformat(), "units_C1": "100", "operator_C1": "Sam",
})
# Jinja autoescapes, so the apostrophe in "isn't" arrives as &#39;.
check("rejected", "a 1st Shift time slot" in res.text, res.text[:300])

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
