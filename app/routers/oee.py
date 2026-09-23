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

And it is NOT for the floor screens. A partial-slot OEE reads terribly at
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

GRAIN: ONE ROW PER MACHINE PER SHIFT
------------------------------------
Downtime and scrap are captured once at the end of a shift on /console/oee, so
there is no per-slot downtime to divide by and the per-slot OEE columns this
page used to carry are gone. That is the right trade rather than a loss: a slot
delta is the gap between two hand-taken readings, and a reading logged late
borrows units from its neighbour — noise that produced false "impossible value"
alarms on seven machines in the first week of use. A shift is 480 minutes
however the readings fell — or 600 or 720 on a machine whose day was set to
10 or 12 hours on /console/oee, or 60 per hour typed on a short day, which
the tag next to its name shows. Per-slot production is still in the payload
and surfaces on the Good column's tooltip, which is what you need to find a
bad checkpoint.

THE PARETO'S PERIODS
--------------------
The downtime Pareto shows the selected shift by default, or the last 7 or 30
production days ending on the date in the date box, all three shifts
together (/api/oee-pareto). Rolling rather than calendar weeks, and anchored
on the date box rather than on today, so it is "the last week" by default and
"the week that ended then" for any date picked. Only the Pareto has periods;
the OEE grid stays one shift, for the reason there's no All Day option.

Read-only, like /supervisor — every write path for scrap, downtime and
scheduling lives on /console/oee. That matters more here than it looks: the
not-scheduled flag REMOVES time from the OEE denominator, so it's the one field
in this system with an incentive to be wrong, and it has no business on an
unauthenticated review page.

Access model matches the rest of the app: no auth, URL obscurity only, per the
architecture doc.
"""
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db.entries import get_available_production_days
from app.db.oee import compute_oee_report, compute_pareto_range
from app.models import (
    DASHBOARD_ZONE_LABELS,
    DASHBOARD_ZONES,
    OEE_ZONE_ORDER,
    PARETO_RANGE_DAYS,
    SHIFT_ORDER,
    SHORT_PATTERN_HOURS,
    STANDARD_PCT_OF_IDEAL,
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


def _parse_period(raw: str | None) -> int | None:
    """The Pareto's period: one of PARETO_RANGE_DAYS, or None for the
    selected shift (the default, and what anything unrecognised means)."""
    try:
        days = int(raw) if raw else None
    except ValueError:
        return None
    return days if days in PARETO_RANGE_DAYS else None


@router.get("/oee", response_class=HTMLResponse)
def oee_page(
    request: Request,
    date: str | None = None,
    shift: str | None = None,
    pareto: str | None = None,
    period: str | None = None,
):
    """Page shell for the OEE grid.

    `date`, `shift`, `pareto` and `period` are optional query params so a
    particular view can be bookmarked or pasted; oee.js keeps them in sync
    with the on-page controls via history.replaceState. None is validated
    into an error here — the shell renders the same either way, /api/oee-data
    resolves a junk date to a sensible one rather than 500ing, and oee.js
    drops machine ids it doesn't know. `pareto` is a comma-separated list of
    machine ids for the downtime Pareto's picker; absent means the whole
    floor. `period` is 7 or 30 for the Pareto's rolling windows; absent
    means the selected shift.

    Note there's no All Day option, unlike /supervisor. OEE is defined against
    Planned Production Time, and the three shifts have separate PPTs, separate
    downtime and separate crews — a single number spanning all three would
    average away the very thing the page exists to show. The shift toggle here
    is three options, not four.

    The date is a PRODUCTION day: its 3rd Shift is the one that starts at
    10PM on it. See "THE PRODUCTION DAY" in app/models.py.
    """
    return templates.TemplateResponse(
        request=request,
        name="oee.html",
        context={
            "zones": _oee_zones(),
            "shift_order": SHIFT_ORDER,
            # Feeds window.STANDARD_PCT_OF_IDEAL, which oee.js derives its
            # colour bands from — "good" starts at standard, so the bands move
            # if the floor ever revises that figure.
            "standard_pct_of_ideal": STANDARD_PCT_OF_IDEAL,
            "requested_date": date or "",
            "requested_shift": resolve_shift(shift, datetime.now()),
            "requested_pareto": pareto or "",
            "requested_period": _parse_period(period),
            "pareto_range_days": PARETO_RANGE_DAYS,
            "short_pattern_hours": SHORT_PATTERN_HOURS,
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
    anyway. They are PRODUCTION days (6AM to 6AM): a morning with only
    3rd-Shift readings so far doesn't show up as a new day yet.
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


@router.get("/api/oee-pareto")
def oee_pareto(end: str | None = None, days: str | None = None):
    """Downtime by reason, per machine, over the `days` production days
    ending on `end` — the Pareto's "7 days" / "30 days" views.

    Rolling and anchored on the page's date box rather than on today, so the
    default (the page opens on the latest day with data) is "the last week",
    and picking an older date shows the week that ended then. All three
    shifts together: the shift toggle is about one shift's OEE, and a week
    of downtime is the floor's. Returned per machine so the page's machine
    picker narrows it without another round trip; see compute_pareto_range().

    Forgiving like /api/oee-data: a missing or junk `end` means the latest
    production day with data, and a `days` that isn't one of the offered
    periods means the shortest one.
    """
    period = _parse_period(days) or PARETO_RANGE_DAYS[0]
    end_day = _parse_date(end)
    if end_day is None:
        available = get_available_production_days()
        end_day = available[0] if available else datetime.now().date()
    return compute_pareto_range(end_day, period)
