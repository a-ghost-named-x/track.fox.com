"""Routes for /dashboard — the public, no-auth display BrightSign renders.

Example here is a "mixed" dashboard per the architecture doc: it merges a
read-only MSSQL query with manual-entry data from Postgres into one view.
Swap or duplicate this pattern for SQL-only or manual-entry-only dashboards.
"""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.entries import get_latest_entries_for_date, get_shift_activity
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
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
def dashboard_index(request: Request):
    """Landing page — lists each floor-section zone as a link rather than
    rendering a single all-machines grid. Point BrightSign at a specific
    /dashboard/<zone> URL directly; this page is for a person browsing on a
    computer to find the right one.
    """
    zones = [
        {
            "slug": slug,
            "label": DASHBOARD_ZONE_LABELS.get(slug, slug.upper()),
            "machine_count": len(machine_ids),
        }
        for slug, machine_ids in DASHBOARD_ZONES.items()
    ]
    return templates.TemplateResponse(
        request=request,
        name="dashboard_index.html",
        context={"zones": zones},
    )


@router.get("/dashboard/{zone}", response_class=HTMLResponse)
def dashboard_zone_page(request: Request, zone: str):
    """Initial page load for one zone's grid (e.g. /dashboard/b3) — renders
    the shell scoped to just that zone's machines; data is filled in via
    polling, same as before. 404s on an unrecognized zone slug rather than
    silently rendering an empty grid.
    """
    machine_ids = DASHBOARD_ZONES.get(zone)
    if machine_ids is None:
        raise HTTPException(status_code=404, detail=f"No dashboard zone named '{zone}'.")

    now = datetime.now()
    shift = get_current_shift(now)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "machine_ids": machine_ids,
            "time_slots": TIME_SLOTS,
            "active_slots": SHIFT_SLOTS[shift],
            "poll_interval_ms": settings.poll_interval_ms,
            "zone_label": DASHBOARD_ZONE_LABELS.get(zone, zone.upper()),
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
    shift = get_current_shift(now)
    active_slots = SHIFT_SLOTS[shift]
    entries = get_latest_entries_for_date(today)
    return {
        "date": today.isoformat(),
        "shift": shift,
        "active_slots": active_slots,
        "entries": entries,
        # Per-machine operator + carried issue for the shift in progress —
        # see get_shift_activity() docstring for the carry-forward rule.
        "machine_activity": get_shift_activity(today, active_slots),
    }
