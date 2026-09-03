"""Routes for /oee — Overall Equipment Effectiveness by machine and shift.

OEE = Availability x Performance x Quality. Where /dashboard answers "what is
the floor doing right now" and /supervisor answers "how did that shift do",
this answers "where did the capacity go" — and, via the downtime Pareto,
"what should we go fix first".

Follows /supervisor's conventions rather than /dashboard's, deliberately and
for the same three reasons spelled out in app/routers/supervisor.py:

  - It doesn't poll. A fixed past date has nothing to poll for.
  - It shows every machine, including ones that never reported. A blank row is
    the finding.
  - It opens on the most recent date that has data, not on today. OEE for a
    shift that's two slots old is mostly noise.

And it is NOT for the BrightSign boards. A partial-slot OEE reads terribly at
8:05AM, and this is a review metric for a person at a desk. Linked from the
/dashboard zone-picker index alongside /supervisor, never from
/dashboard/<zone>.

THE NUMBER PEOPLE WILL ASK ABOUT
--------------------------------
A machine hitting standard exactly, with no scrap and no downtime, scores 75%
OEE — not 100%. Standards are set at 75% of theoretical maximum, so the
remaining 25% is a genuine Performance loss against the machine's physical
ceiling. That's why the page shows "% of standard" next to OEE: the first
column is the metric the floor already trusts from the :) / :( boards, the
second is the one that benchmarks against 85%-is-world-class. They differ by
exactly the 0.75 factor and neither is hiding anything. See
docs/sql/08_seed_ideal_rates.sql for the derivation.

Read-only, like /supervisor — every write path for scrap, downtime and
scheduling lives on /console, which is the surface that already takes writes.
That matters more here than it looks: the not-scheduled flag REMOVES time from
the OEE denominator, so it's the one field in this system with an incentive to
be wrong, and it has no business on an unauthenticated review page.

Access model matches the rest of the app: no auth, URL obscurity only, per the
architecture doc.
"""
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db.entries import get_available_entry_dates
from app.db.oee import compute_oee_report
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    OEE_ZONE_ORDER,
    SHIFT_ORDER,
    SHIFT_SLOTS,
    STANDARD_PCT_OF_IDEAL,
    TIME_SLOTS,
    resolve_shift,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _oee_zones() -> list[dict]:
    """Zone sections to render, top to bottom, per OEE_ZONE_ORDER.

    A slug in the order list that's missing from DASHBOARD_ZONES is skipped
    rather than raising, so retiring a zone can't 500 this page — same
    defensive shape as _supervisor_zones().
    """
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


@router.get("/oee", response_class=HTMLResponse)
def oee_page(request: Request, date: str | None = None, shift: str | None = None):
    """Page shell for the OEE grid.

    `date` and `shift` are optional query params so a particular view can be
    bookmarked or pasted; oee.js keeps them in sync with the on-page controls
    via history.replaceState. Neither is validated into an error here — the
    shell renders the same either way and /api/oee-data resolves a junk date
    to a sensible one rather than 500ing.

    Note there's no All Day option, unlike /supervisor. OEE is defined against
    Planned Production Time, and the three shifts have separate PPTs, separate
    downtime and separate crews — a single number spanning all three would
    average away the very thing the page exists to show. The shift toggle here
    is three options, not four.
    """
    return templates.TemplateResponse(
        request=request,
        name="oee.html",
        context={
            "zones": _oee_zones(),
            "time_slots": TIME_SLOTS,
            "shift_order": SHIFT_ORDER,
            "shift_slots": SHIFT_SLOTS,
            # Feeds window.STANDARD_PCT_OF_IDEAL, which oee.js derives its
            # colour bands from — "good" starts at standard, so the bands move
            # if the floor ever revises that figure.
            "standard_pct_of_ideal": STANDARD_PCT_OF_IDEAL,
            "requested_date": date or "",
            "requested_shift": resolve_shift(shift, datetime.now()),
        },
    )


@router.get("/api/oee-data")
def oee_data(date: str | None = None):
    """OEE for one production date — every machine, all three shifts.

    All three shifts ship in one payload so the toggle switches client-side
    with no round trip, the same trade /api/supervisor-data makes and
    affordable for the same reason: this page doesn't poll.

    Date resolution is forgiving in one direction only, matching
    /api/supervisor-data. A missing or unparseable date falls back to the most
    recent date with entries; a date that parses fine but has no entries is
    honoured as-is and comes back empty, because bouncing someone off the
    quiet Sunday they deliberately picked is more confusing than showing them
    that it was quiet.

    available_dates comes from `entries`, not from the scrap or downtime
    tables. Production is the spine — a date with downtime logged but no units
    isn't a production day worth reviewing, and OEE can't be computed for it
    anyway.
    """
    available = get_available_entry_dates()
    requested = _parse_date(date)
    resolved = requested if requested is not None else (
        available[0] if available else datetime.now().date()
    )

    report = compute_oee_report(resolved)
    report["available_dates"] = [d.isoformat() for d in available]
    report["has_any_data"] = bool(available)
    return report
