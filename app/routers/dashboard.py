"""Routes for /dashboard — the public, no-auth display BrightSign renders.

Example here is a "mixed" dashboard per the architecture doc: it merges a
read-only MSSQL query with manual-entry data from Postgres into one view.
Swap or duplicate this pattern for SQL-only or manual-entry-only dashboards.
"""
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.entries import get_latest_entries_for_date
from app.models import (
    MACHINE_IDS,
    SHIFT_DISPLAY_DELAY_HOURS,
    SHIFT_SLOTS,
    SHIFTS,
    TIME_SLOTS,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_current_shift(now: datetime) -> str:
    """Returns the label of whichever shift the dashboard should display at
    `now`, using server-local time (see SHIFTS in app.models). Display-only.

    Each shift's real start hour is pushed back by SHIFT_DISPLAY_DELAY_HOURS
    before comparing, so the dashboard keeps showing the outgoing shift for
    that many hours past its real changeover — giving the incoming crew time
    to review the outgoing shift's production before the display switches.

    SHIFTS is sorted by start hour; we walk it and keep the last (delayed)
    boundary that `now` has passed. Hours before the first boundary fall
    through to the final shift in the list, since that shift wraps past
    midnight (10PM-6AM).
    """
    current = SHIFTS[-1][1]
    for start_hour, label in SHIFTS:
        display_hour = (start_hour + SHIFT_DISPLAY_DELAY_HOURS) % 24
        if now.hour >= display_hour:
            current = label
    return current


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """Initial page load — renders the shell; data is filled in via polling."""
    now = datetime.now()
    shift = get_current_shift(now)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "machine_ids": MACHINE_IDS,
            "time_slots": TIME_SLOTS,
            "active_slots": SHIFT_SLOTS[shift],
            "poll_interval_ms": settings.poll_interval_ms,
        },
    )


@router.get("/api/dashboard-data")
def dashboard_data():
    """JSON endpoint the dashboard page polls periodically (see static/js/dashboard.js).

    Returns today's manual entries, pivoted by the frontend into the grid,
    plus which 4 time-slot columns are relevant to the shift in progress
    right now (so the grid can narrow down from 12 columns to 4 without a
    page reload if the shift changes while the page stays open).

    TODO: this is where a SQL-only or mixed dashboard would also query MSSQL
    via app.db.mssql.run_readonly_query(...) and merge results before
    returning — left out here since the production query itself depends on
    your actual MSSQL schema, which this scaffold doesn't have visibility into.
    """
    now = datetime.now()
    today = now.date()
    entries = get_latest_entries_for_date(today)
    shift = get_current_shift(now)
    return {
        "date": today.isoformat(),
        "shift": shift,
        "active_slots": SHIFT_SLOTS[shift],
        "entries": entries,
    }
