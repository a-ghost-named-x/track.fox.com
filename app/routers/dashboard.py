"""Routes for /dashboard: the site menu and the floor screens."""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.entries import get_latest_entries_for_date, get_shift_activity
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    SHIFT_SLOTS,
    TIME_SLOTS,
    get_current_shift,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_index(request: Request):
    """Site menu. Floor screens point directly at /dashboard/<zone>; this
    page is for people browsing."""
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
    """Page shell for one zone's grid (e.g. /dashboard/b3). The data is
    filled in by polling. Unknown zones return 404."""
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
    """JSON the dashboard polls (see static/js/dashboard.js).

    Returns today's entries plus the current shift's four slot columns, so
    the grid can switch shifts without a page reload.
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
        # Per-machine operator and issues for the current shift.
        "machine_activity": get_shift_activity(today, active_slots),
    }
