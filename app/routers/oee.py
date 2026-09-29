"""Routes for /oee: Overall Equipment Effectiveness by machine and shift.

OEE = Availability x Performance x Quality, one row per machine per shift,
plus a downtime Pareto. Like /supervisor, it doesn't poll, shows every
machine, and opens on the most recent date with data. It's a review page,
not a floor screen.

A machine that hits standard exactly with no scrap or downtime scores 75%,
not 100%, because standards are set at 75% of theoretical maximum. That's
why "% of standard" is shown next to OEE.

A shift is 480 minutes however the readings fell (600 or 720 for a 10 or
12-hour shift, 60 per scheduled hour on a short day). Per-slot production is
still in the payload for the Good column's tooltip.

The whole page, grid and Pareto, can also show the last 7 or 30 production
days ending on the selected date, compared with the 7 or 30 before them, for
all shifts together or one at a time (/api/oee-range).

Read-only. All OEE inputs are written on /console/oee.
"""
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db.entries import get_available_production_days
from app.db.oee import compute_oee_range, compute_oee_report
from app.models import (
    ALL_SHIFTS_LABEL,
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    OEE_PERIOD_DAYS,
    OEE_ZONE_ORDER,
    SHIFT_ORDER,
    SHORT_PATTERN_HOURS,
    STANDARD_PCT_OF_IDEAL,
    resolve_shift,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _oee_zones() -> list[dict]:
    """Zone sections to render, top to bottom, per OEE_ZONE_ORDER."""
    zones = []
    for slug in OEE_ZONE_ORDER:
        machine_ids = DASHBOARD_ZONES.get(slug)
        if machine_ids is None:
            continue
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


def _parse_period(raw: str | None) -> int | None:
    """The page's period: one of OEE_PERIOD_DAYS, or None for one shift (also
    used for anything unrecognised)."""
    try:
        days = int(raw) if raw else None
    except ValueError:
        return None
    return days if days in OEE_PERIOD_DAYS else None


@router.get("/oee", response_class=HTMLResponse)
def oee_page(
    request: Request,
    date: str | None = None,
    shift: str | None = None,
    pareto: str | None = None,
    period: str | None = None,
):
    """Page shell for the OEE grid.

    Query params (all optional, kept in sync by oee.js so a view can be
    bookmarked):
      date   - production day; its 3rd Shift starts at 10PM that day. Over 7
               or 30 days, the last day of the period.
      shift  - which shift to show, or "All" over 7 or 30 days (the default
               there)
      pareto - comma-separated machine ids for the Pareto; absent = whole floor
      period - 7 or 30 for a rolling window; absent = one shift

    One shift has no "All" option: each shift has its own planned time,
    downtime and crew, and one number across a single day's three would hide
    the differences. Over a period, All is shown with each shift's OEE beside
    it for the same reason.
    """
    days = _parse_period(period)
    default_shift = resolve_shift(None, datetime.now())
    if days and shift in (None, ALL_SHIFTS_LABEL):
        requested_shift = ALL_SHIFTS_LABEL
    else:
        requested_shift = resolve_shift(shift, datetime.now())

    return templates.TemplateResponse(
        request=request,
        name="oee.html",
        context={
            "zones": _oee_zones(),
            "shift_order": SHIFT_ORDER,
            # oee.js derives its colour bands from this ("good" starts at
            # standard).
            "standard_pct_of_ideal": STANDARD_PCT_OF_IDEAL,
            "requested_date": date or "",
            "requested_shift": requested_shift,
            # The single shift to show when leaving a period opened on All.
            "default_shift": default_shift,
            "requested_pareto": pareto or "",
            "requested_period": days,
            "period_days": OEE_PERIOD_DAYS,
            "all_shifts_label": ALL_SHIFTS_LABEL,
            "short_pattern_hours": SHORT_PATTERN_HOURS,
        },
    )


@router.get("/api/oee-data")
def oee_data(date: str | None = None):
    """OEE for one production date: every machine, all three shifts.

    A missing or unparseable date falls back to the most recent production
    day with entries. A valid date with no entries is returned as-is, empty.
    available_dates comes from `entries`, since OEE can't be computed without
    production.
    """
    available = get_available_production_days()
    requested = _parse_date(date)
    resolved = requested if requested is not None else (
        available[0] if available else datetime.now().date()
    )

    report = compute_oee_report(resolved)
    report["available_dates"] = [d.isoformat() for d in available]
    report["has_any_data"] = bool(available)
    return report


@router.get("/api/oee-range")
def oee_range(end: str | None = None, days: str | None = None):
    """OEE over the `days` production days ending on `end`, and the `days`
    before them, for the page's 7 and 30-day views. Per machine, per zone and
    for the floor, all shifts together and each shift, plus each machine's
    Pareto reasons.

    A missing or invalid `end` means the latest production day with data; an
    unrecognised `days` means the shortest period.
    """
    period = _parse_period(days) or OEE_PERIOD_DAYS[0]
    end_day = _parse_date(end)
    if end_day is None:
        available = get_available_production_days()
        end_day = available[0] if available else datetime.now().date()
    return compute_oee_range(end_day, period)
