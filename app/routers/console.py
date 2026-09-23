"""Routes for /console, the 2-hour production entry forms.

There's no login; access is by URL only.

  - /console: one machine per submit, for one-off entries and corrections.
    A correction is a new row for a machine and slot already logged.
  - /console/<zone>: employee number, date, shift and time slot entered once,
    then every machine in the zone on one page, saved together.

Operator is a per-machine field on the batch form because operators differ
between machines and rotate mid-shift. The GET pre-fills it with whoever was
last logged on that machine this shift, so it usually only needs typing once
per shift.

The batch form's shift and date are chosen by the user, defaulting to the
current shift and today. The form must not follow the dashboard's display
delay, which rolls to 2nd Shift at 3PM while 1st Shift numbers are often
still being entered. The date box is mainly for 3rd Shift (whose slots fall
on the next calendar day) and next-morning corrections. entry_date decides
which day's grid an entry appears on.
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

    The GET page falls back to today; the POST shows an error rather than
    filing entries under a guessed day.
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
        # StandardNotFoundError messages are readable as-is. Pydantic's
        # ValidationError text isn't, so it gets a generic message.
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
                # Keep the date they picked rather than resetting to today.
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
    """Template context for the batch page, shared by the GET and the POST.

    `shift` and `entry_date` come from the caller (the user's choice), never
    from the clock. current_shift is included so the template can point out
    when the two differ.
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
    """Batch entry form for one zone (e.g. /console/b3).

    `shift` and `entry_date` come from the page's shift toggle and date box
    (e.g. ?shift=1st+Shift&entry_date=2026-07-28) and default to now. The
    selected shift drives the time-slot dropdown.

    Each operator box is pre-filled with the last operator logged for that
    machine in the selected shift. Units and issue are never pre-filled.
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
    """Saves every machine row with a units value, sharing one employee
    number, time slot and date.

    Per-machine fields are named operator_<id>, units_<id> and issue_<id>,
    so they're read from request.form().

    Each row is saved on its own, so one bad row (no operator, no standard)
    doesn't block the rest. Rows with no units are skipped, not errors.
    """
    machine_ids = DASHBOARD_ZONES.get(zone)
    if machine_ids is None:
        raise HTTPException(status_code=404, detail=f"No dashboard zone named '{zone}'.")

    form = await request.form()
    entered_by = (form.get("entered_by") or "").strip()
    time_slot = (form.get("time_slot") or "").strip()

    # The shift the page was showing, from a hidden field. Re-resolved so an
    # invalid value can't index SHIFT_SLOTS.
    selected_shift = resolve_shift((form.get("shift") or "").strip(), datetime.now())
    selected_date = _parse_entry_date(form.get("entry_date"))

    # Check the shared fields once, so a missing one gives one message rather
    # than the same error on every row.
    #
    # The slot must belong to the selected shift, or the numbers would land
    # on a column nobody looks at for that shift. The dropdown prevents this,
    # so it only trips on a stale form or a hand-built POST.
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
