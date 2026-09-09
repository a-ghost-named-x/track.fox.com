"""Routes for /console — the manual data-entry form.

Gated only by URL obscurity, not credentials, per the architecture doc's
access-model decision. Do not add auth logic here without revisiting that
decision deliberately.

Two entry modes live here:
  - /console: one machine per submit. Kept around for one-off entries and
    corrections (append a new row for a machine+slot already logged).
  - /console/<zone> (added later, see console_batch_page/console_batch_submit
    below): employee number and time slot entered once, then every machine
    in that zone on one page with a units-produced box each, saved together.
    Built specifically because routine zone-by-zone entry through the
    one-machine-at-a-time form was taking ~40 minutes for a 14-machine zone
    — almost entirely retyping the same employee number and reselecting the
    same time slot over and over.
    Operator is deliberately NOT a shared field here, even though everything
    else is — machines in the same zone can have different operators
    running them, and operators rotate mid-shift. It's stored fresh per
    entry (see app/db/entries.py: nothing carries an operator value forward
    automatically on the *save* side, on this form or the single-entry one),
    so it has to stay a per-machine field here.

    It IS pre-filled on the *load* side, though: console_batch_page (GET)
    seeds each row's operator box with whoever was most recently logged for
    that machine so far this shift, via get_shift_activity() — the same
    lookup the dashboard's operator column already uses. So in the common
    case (same operator all shift) it only needs typing once, at whichever
    slot is entered first; every later round in the shift shows it already
    filled in, still editable if someone rotated onto that machine.

Which shift and date the batch form is logging against are the user's to
pick, not the clock's. Both pages used to derive everything from
datetime.now(), which was fine for the dashboard but wrong here: the
dashboard's one-hour changeover delay (SHIFT_DISPLAY_DELAY_HOURS) rolls the
display to 2nd Shift at 3PM, and because this form read the same
get_current_shift(), 1st Shift's time slots vanished from its dropdown at
3PM too — locking people out of entering 1st Shift numbers that routinely
aren't collected until 4PM. The shift toggle and date box on
console_batch.html fix that; they default to the current shift and today,
so the common case is unchanged, and resolve_shift() in app/models.py keeps
the console's choice independent of the dashboard's display rule.

The date box matters most for 3rd Shift, whose slots (12AM-6AM) fall on the
calendar day *after* the shift starts — and for anyone correcting the
previous day's numbers the next morning. entry_date is what /dashboard
filters on, so the date in that box decides which day's grid an entry lands
on.
"""
from datetime import date, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.db.entries import StandardNotFoundError, create_entry, get_shift_activity
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    MACHINE_IDS,
    SHIFT_ORDER,
    SHIFT_SLOTS,
    TIME_SLOTS,
    EntryCreate,
    get_current_shift,
    resolve_shift,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _parse_entry_date(raw: str | None) -> date | None:
    """Parses the date box's value, or None if it's missing/unparseable.

    Callers decide what to do with None: the GET page quietly falls back to
    today (a junk date in a URL shouldn't 500 a floor tablet), while the POST
    handler turns it into a visible error rather than silently filing the
    entries under the wrong day.
    """
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _zone_links() -> list[dict]:
    """Zone summaries for the plain /console page to link out to each
    zone's faster batch entry form."""
    return [
        {"slug": slug, "label": DASHBOARD_ZONE_LABELS.get(slug, slug.upper())}
        for slug in DASHBOARD_ZONES
    ]


@router.get("/console", response_class=HTMLResponse)
def console_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="console.html",
        context={
            "machine_ids": MACHINE_IDS,
            "time_slots": TIME_SLOTS,
            "entry_date": date.today().isoformat(),
            "error": None,
            "zones": _zone_links(),
        },
    )


@router.post("/console", response_class=HTMLResponse)
def console_submit(
    request: Request,
    machine_id: str = Form(...),
    operator: str = Form(...),
    time_slot: str = Form(...),
    units_produced: int = Form(...),
    issue: str = Form(""),
    entered_by: str = Form(...),
    entry_date: str = Form(...),
):
    try:
        payload = EntryCreate(
            machine_id=machine_id,
            operator=operator,
            time_slot=time_slot,
            units_produced=units_produced,
            issue=issue or None,
            entered_by=entered_by,
            entry_date=entry_date,
        )
        create_entry(payload)
    except (StandardNotFoundError, ValidationError) as exc:
        # StandardNotFoundError messages are already human-readable (see
        # app/db/entries.py). ValidationError's default text is Pydantic
        # internals (field names, type codes) — not something a floor
        # operator should have to parse, so it gets a generic message instead.
        error_message = (
            str(exc) if isinstance(exc, StandardNotFoundError)
            else "Couldn't save that entry — please check the form and try again."
        )
        return templates.TemplateResponse(
            request=request,
            name="console.html",
            context={
                "machine_ids": MACHINE_IDS,
                "time_slots": TIME_SLOTS,
                # Echo back the date they picked rather than resetting to
                # today — otherwise correcting an unrelated field silently
                # re-points the entry at the wrong day.
                "entry_date": entry_date,
                "error": error_message,
                "zones": _zone_links(),
            },
            status_code=400,
        )

    return RedirectResponse(url="/console?submitted=1", status_code=303)


def _batch_context(
    zone: str,
    machine_ids: list[str],
    shift: str,
    entry_date: date,
    *,
    rows: dict[str, dict] | None = None,
    entered_by: str = "",
    time_slot: str = "",
    top_error: str | None = None,
):
    """Shared context-builder for the batch page's GET (fresh form) and POST
    (re-rendered with per-row results) responses — keeps both in sync on
    what the template expects.

    `shift` and `entry_date` are resolved by the caller and passed in rather
    than recomputed from the clock here: both are user-controlled now (the
    shift toggle and the date box), and this function runs on the POST path
    too, where quietly re-deriving either one from datetime.now() would mean
    a page that saved 1st Shift's 2PM numbers at 4PM re-rendered itself as
    2nd Shift.

    current_shift goes into the context alongside the selected one purely so
    the template can point out when they differ.

    saved_count/failed_rows are pre-computed here (rather than in the
    template via Jinja filters) since dict.items() tuples don't support the
    attribute-style access selectattr() needs — plain Python is simpler and
    less fragile for this than a filter chain would be.
    """
    saved_count = 0
    failed_rows: list[tuple[str, dict]] = []
    if rows:
        for machine_id, row in rows.items():
            if row["status"] == "saved":
                saved_count += 1
            elif row["status"] == "failed":
                failed_rows.append((machine_id, row))

    return {
        "zone": zone,
        "zone_label": DASHBOARD_ZONE_LABELS.get(zone, zone.upper()),
        "machine_ids": machine_ids,
        "shift": shift,
        "current_shift": get_current_shift(datetime.now()),
        "shift_options": SHIFT_ORDER,
        "active_slots": SHIFT_SLOTS[shift],
        "entry_date": entry_date.isoformat(),
        "rows": rows,
        "saved_count": saved_count,
        "failed_rows": failed_rows,
        "entered_by": entered_by,
        "time_slot": time_slot,
        "top_error": top_error,
    }


@router.get("/console/{zone}", response_class=HTMLResponse)
def console_batch_page(request: Request, zone: str, shift: str | None = None, entry_date: str | None = None):
    """Fresh batch entry form for one zone (e.g. /console/b3) — one row per
    machine in that zone with its own operator field, employee number/time
    slot entered once for the whole page. 404s on an unrecognized zone slug,
    same as the matching /dashboard/{zone} route.

    `shift` and `entry_date` are what the page's shift toggle and date box
    put in the URL (e.g. /console/b3?shift=1st+Shift&entry_date=2026-07-28).
    Both are optional and both fall back to "right now" when absent or
    unparseable, so a bare /console/b3 still opens on the shift in progress
    and today's date exactly like it did before the toggle existed.

    Whichever shift is selected drives the time-slot dropdown, so 1st
    Shift's slots stay reachable after the 3PM dashboard changeover — the
    lockout this toggle exists to fix.

    Each row's operator box is pre-filled from get_shift_activity() — "who
    was most recently entered for this machine, anywhere in the *selected*
    shift on the selected date" — so the second, third, and fourth round of
    entries in a shift start with the operator already in place instead of
    blank. First round of a shift has nothing to pull from yet, so it
    renders blank as before. Units/issue are never pre-filled — only
    operator carries this way.
    """
    machine_ids = DASHBOARD_ZONES.get(zone)
    if machine_ids is None:
        raise HTTPException(status_code=404, detail=f"No dashboard zone named '{zone}'.")

    selected_shift = resolve_shift(shift, datetime.now())
    selected_date = _parse_entry_date(entry_date) or date.today()
    activity = get_shift_activity(selected_date, SHIFT_SLOTS[selected_shift])
    prefill_rows = {
        machine_id: {
            "operator": activity.get(machine_id, {}).get("operator") or "",
            "units": "",
            "issue": "",
            "status": None,
            "error": None,
        }
        for machine_id in machine_ids
    }

    return templates.TemplateResponse(
        request=request,
        name="console_batch.html",
        context=_batch_context(
            zone, machine_ids, selected_shift, selected_date, rows=prefill_rows,
        ),
    )


@router.post("/console/{zone}", response_class=HTMLResponse)
async def console_batch_submit(request: Request, zone: str):
    """Saves every machine row that has a units-produced value filled in,
    sharing one employee number/time slot/date across all of them — but
    NOT operator, which is entered per machine (see module docstring for why).

    Per-machine fields arrive as dynamically-named form fields (operator_<id>,
    units_<id>, issue_<id>) rather than fixed Form(...) parameters, since the
    set of machines varies by zone — read via request.form() instead.

    Each row is saved independently rather than as one all-or-nothing
    transaction: a machine with no standard yet (a newly added machine
    listed in MACHINE_IDS before its standards rows are seeded), or one
    that's simply missing its operator name, shouldn't block the other
    machines in the zone from saving. Blank rows (no units entered) are
    just skipped, not treated as errors — it's normal for a machine to
    have nothing to report yet.
    """
    machine_ids = DASHBOARD_ZONES.get(zone)
    if machine_ids is None:
        raise HTTPException(status_code=404, detail=f"No dashboard zone named '{zone}'.")

    form = await request.form()
    entered_by = (form.get("entered_by") or "").strip()
    time_slot = (form.get("time_slot") or "").strip()

    # Which shift the page was showing when it was submitted, carried in a
    # hidden field. Re-resolved rather than trusted so a tampered/stale value
    # can't index SHIFT_SLOTS with something that isn't a shift.
    selected_shift = resolve_shift((form.get("shift") or "").strip(), datetime.now())
    selected_date = _parse_entry_date(form.get("entry_date"))

    # Validate the shared fields once up front — if these are missing,
    # every row would fail with the same identical error, which is noise,
    # not information. One clear message instead.
    #
    # The time_slot/shift cross-check matters more than it looks: the two are
    # picked from separate controls, and saving a slot against the wrong
    # shift would file real production numbers under a slot nobody looks at
    # on that shift's grid. The dropdown only ever offers the selected
    # shift's slots, so this only trips on a stale form or a hand-built POST.
    if selected_date is None:
        top_error = "Enter a valid date before saving."
    elif not entered_by or not time_slot:
        top_error = "Fill in employee number and time slot before saving."
    elif time_slot not in SHIFT_SLOTS[selected_shift]:
        top_error = f"{time_slot} isn't a {selected_shift} time slot — pick the shift you're logging, then the slot."
    else:
        top_error = None

    if top_error is not None:
        return templates.TemplateResponse(
            request=request,
            name="console_batch.html",
            context=_batch_context(
                zone, machine_ids, selected_shift, selected_date or date.today(),
                entered_by=entered_by, time_slot=time_slot,
                top_error=top_error,
            ),
            status_code=400,
        )

    rows: dict[str, dict] = {}
    any_submitted = False

    for machine_id in machine_ids:
        raw_operator = (form.get(f"operator_{machine_id}") or "").strip()
        raw_units = (form.get(f"units_{machine_id}") or "").strip()
        raw_issue = (form.get(f"issue_{machine_id}") or "").strip()
        row = {"operator": raw_operator, "units": raw_units, "issue": raw_issue, "status": None, "error": None}
        rows[machine_id] = row

        if not raw_units:
            row["status"] = "skipped"
            continue

        any_submitted = True

        if not raw_operator:
            row["status"] = "failed"
            row["error"] = "Operator is required."
            continue

        try:
            units_produced = int(raw_units)
        except ValueError:
            row["status"] = "failed"
            row["error"] = "Units must be a whole number."
            continue

        try:
            payload = EntryCreate(
                machine_id=machine_id,
                operator=raw_operator,
                time_slot=time_slot,
                units_produced=units_produced,
                issue=raw_issue or None,
                entered_by=entered_by,
                entry_date=selected_date,
            )
            create_entry(payload)
            row["status"] = "saved"
        except (StandardNotFoundError, ValidationError) as exc:
            row["status"] = "failed"
            row["error"] = (
                str(exc) if isinstance(exc, StandardNotFoundError)
                else "Couldn't save — check the value and try again."
            )

    top_error = None if any_submitted else "Nothing to save — enter units for at least one machine."
    return templates.TemplateResponse(
        request=request,
        name="console_batch.html",
        context=_batch_context(
            zone, machine_ids, selected_shift, selected_date,
            rows=rows, entered_by=entered_by, time_slot=time_slot,
            top_error=top_error,
        ),
        status_code=200 if any_submitted else 400,
    )
