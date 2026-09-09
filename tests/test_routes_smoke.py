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

# The floor's real eleven, all unplanned (docs/sql/12_seed_shift_downtime_reasons.sql).
REASONS = {
    code: {"code": code, "label": label, "is_planned": False}
    for code, label in [
        ("ROLL_CHANGE", "Roll Change"), ("SETUP", "Setup"), ("FAAR", "FAAR"),
        ("EQUIP_FAIL", "Equipment Failure"), ("OPER_ADJUST", "Operator Adjustments"),
        ("DEFECT_MAT", "Defective Material"), ("LACK_MAT", "Lack of Material"),
        ("LACK_OPER", "Lack of Operator"), ("DELIVERY", "Delivery"),
        ("REGISTRATION", "Registration"), ("SHIFT_START", "Start of Shift"),
    ]
}

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
entries_mod.get_shift_activity = lambda d, slots: {"C1": {"operator": "Sam", "issues": []}}
console_mod.get_shift_activity = entries_mod.get_shift_activity
sup_mod.get_available_entry_dates = entries_mod.get_available_entry_dates
sup_mod.get_latest_entries_for_date = entries_mod.get_latest_entries_for_date
sup_mod.get_shift_activity = entries_mod.get_shift_activity
oee_router.get_available_entry_dates = entries_mod.get_available_entry_dates

stored_scrap = {}
stored_downtime = {}
stored_schedule = {}

for mod in (oee_mod, console_oee_mod):
    mod.get_latest_scrap_for_date = lambda d: dict(stored_scrap)
    mod.get_latest_downtime_for_date = lambda d: dict(stored_downtime)
    mod.get_schedule_for_date = lambda d: dict(stored_schedule)
    mod.get_downtime_reasons = lambda active_only=True: dict(REASONS)
oee_mod.get_ideal_rates = lambda: {m: i / 1.5 for m, i in INCREMENTS.items()}
oee_mod.get_shift_standards = lambda: {
    (m, s): i * 4 for m, i in INCREMENTS.items() for s in SHIFT_SLOTS
}

# --- capture every write ---------------------------------------------------
written = {"entry": [], "scrap": [], "downtime": [], "schedule": []}
console_mod.create_entry = lambda p: written["entry"].append(p) or 1
console_oee_mod.create_shift_scrap = lambda p: written["scrap"].append(p) or 1
console_oee_mod.create_schedule_exception = lambda p: written["schedule"].append(p) or 1


def fake_downtime(payload):
    total = sum(r.minutes for r in payload.reasons)
    if total > 480:
        raise oee_mod.DowntimeExceedsShiftError(
            f"{total} minutes of downtime doesn't fit in a 480-minute shift "
            f"({payload.machine_id}, {payload.shift})."
        )
    unknown = [r.reason_code for r in payload.reasons if r.reason_code not in REASONS]
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
check("all 11 reasons on C1", sum(
    1 for code in REASONS if f'name="dt_on_C1_{code}"' in form) == 11)
check("each reason has a minutes box",
      form.count('name="dt_min_C1_') == 11, str(form.count('name="dt_min_C1_')))
check("reason labels rendered", "Lack of Operator" in form and "FAAR" in form)
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
check("machines carry no per-slot OEE",
      "slots" not in payload["shifts"][SHIFT]["machines"]["C1"])
check("but do carry the checkpoint sequence",
      len(payload["shifts"][SHIFT]["machines"]["C1"]["checkpoints"]) == 4)

print("\n== /dashboard is the site menu ==")
index = client.get("/dashboard").text
for link in ("/console", "/console/oee", "/supervisor", "/oee",
             "/dashboard/b3", "/console/b3"):
    check(f"links to {link}", f'href="{link}"' in index)

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
