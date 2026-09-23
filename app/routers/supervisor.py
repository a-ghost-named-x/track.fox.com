"""Routes for /supervisor, the shift-review page.

Same data and status rule as /dashboard, for looking back at a past shift.
Differences from the floor screens:

  - It doesn't poll; it re-fetches only when the date changes.
  - It shows every machine, including ones with no entries. On a live board
    an empty row is clutter; in a review, it's the thing to look for.
  - It opens on the most recent date with data rather than today.

Numbers are latest-per-slot with corrections included, so this is the
current best-known picture of the shift, not a snapshot of what the board
showed at the time.

Dates are production days: "3rd Shift, Thursday" is Thursday 10PM to Friday
6AM, and "All Day" is 6AM to 6AM. The night slots are read from the next
calendar date (see "The production day" in app/models.py).

Read-only, with no write paths.
"""
from datetime import date as date_type
from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db.entries import (
    get_available_production_days,
    get_production_day_entries,
    get_shift_activity,
)
from app.models import (
    ALL_DAY_LABEL,
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    SHIFT_ORDER,
    SHIFT_SLOTS,
    SUPERVISOR_ZONE_ORDER,
    TIME_SLOTS,
    resolve_shift,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _supervisor_zones() -> list[dict]:
    """All zone sections, top to bottom, per SUPERVISOR_ZONE_ORDER."""
    zones = []
    for slug in SUPERVISOR_ZONE_ORDER:
        machine_ids = DASHBOARD_ZONES.get(slug)
        if machine_ids is None:
            continue  # zone no longer exists
        zones.append(
            {
                "slug": slug,
                "label": DASHBOARD_ZONE_LABELS.get(slug, slug.upper()),
                "machine_ids": machine_ids,
            }
        )
    return zones


def _parse_date(raw: str | None) -> date_type | None:
    """Parses a YYYY-MM-DD query param, or None if missing/unparseable."""
    if not raw:
        return None
    try:
        return date_type.fromisoformat(raw)
    except ValueError:
        return None


@router.get("/supervisor", response_class=HTMLResponse)
def supervisor_page(request: Request, date: str | None = None, shift: str | None = None):
    """Page shell for the shift-review grid.

    `date` and `shift` are optional query params so a view can be
    bookmarked; supervisor.js keeps them in sync with the controls. Invalid
    values aren't errors: /api/supervisor-data falls back to a sensible date.
    """
    now = datetime.now()
    return templates.TemplateResponse(
        request=request,
        name="supervisor.html",
        context={
            "zones": _supervisor_zones(),
            "time_slots": TIME_SLOTS,
            "shift_order": SHIFT_ORDER,
            "shift_slots": SHIFT_SLOTS,
            "all_day_label": ALL_DAY_LABEL,
            "requested_date": date or "",
            # "All Day" isn't a shift, so resolve_shift() would reject it.
            "requested_shift": (
                ALL_DAY_LABEL if shift == ALL_DAY_LABEL else resolve_shift(shift, now)
            ),
        },
    )


@router.get("/api/supervisor-data")
def supervisor_data(date: str | None = None):
    """Everything /supervisor needs for one production date.

    Returns all 12 slots plus each shift's operators and issues, so the shift
    toggle works without another request (at most 408 rows).

    A missing or unparseable date falls back to the most recent date with
    entries. A valid date with no entries is returned as-is, empty.
    """
    available = get_available_production_days()
    requested = _parse_date(date)

    if requested is None:
        resolved = available[0] if available else datetime.now().date()
    else:
        resolved = requested

    entries = get_production_day_entries(resolved)

    # One get_shift_activity() call per shift, which is fine for a page that
    # doesn't poll. The 3rd Shift's slots are filed under the next calendar
    # date, so its activity is read from there. This page is always the
    # 8-hour grid; shift lengths only matter on /oee.
    shift_activity = {
        label: get_shift_activity(
            resolved + timedelta(days=1) if label == SHIFT_ORDER[-1] else resolved,
            SHIFT_SLOTS[label],
        )
        for label in SHIFT_ORDER
    }

    return {
        "date": resolved.isoformat(),
        "available_dates": [d.isoformat() for d in available],
        "has_any_data": bool(available),
        "entries": entries,
        "shift_activity": shift_activity,
    }
