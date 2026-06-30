"""Routes for /console — the manual data-entry form.

Gated only by URL obscurity, not credentials, per the architecture doc's
access-model decision. Do not add auth logic here without revisiting that
decision deliberately.

Adding a test for github actions runner
"""
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.db.entries import StandardNotFoundError, create_entry
from app.models import MACHINE_IDS, TIME_SLOTS, EntryCreate

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/console", response_class=HTMLResponse)
def console_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="console.html",
        context={
            "machine_ids": MACHINE_IDS,
            "time_slots": TIME_SLOTS,
            "today": date.today().isoformat(),
            "error": None,
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
                "today": date.today().isoformat(),
                "error": error_message,
            },
            status_code=400,
        )

    return RedirectResponse(url="/console?submitted=1", status_code=303)
