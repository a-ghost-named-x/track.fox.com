"""Routes for /supervisor — the historical shift-review page.

Where /dashboard answers "what is the floor doing right now" on a BrightSign
screen, this answers "how did a given shift actually do" for a person at a
desk, after the fact. Same underlying data and the same status rule, with
three deliberate behavioural differences from the dashboard:

  - It doesn't poll. /dashboard re-fetches every POLL_INTERVAL_MS because
    it's a live display; a fixed past date has nothing to poll for. This page
    loads once and re-fetches only when the date changes.
  - It shows every machine in a zone, including ones with no entries at all.
    /dashboard hides those (applyMachineVisibility in static/js/dashboard.js)
    because a blank row is noise on a live board. Here it's the opposite: a
    machine that reported nothing all shift is exactly what a supervisor is
    looking for, so the blank row IS the signal.
  - It opens on the most recent date that has data, not on today. At 7AM,
    before the first entries land, defaulting to today would open on an
    empty grid.

The numbers are latest-per-slot, corrections included — the same rule
/dashboard uses via get_latest_entries_for_date(). So this is NOT a frozen
photograph of what the board displayed at 2PM; it's the current best-known
picture of that shift, including a correction filed hours after it ended.
That's the more useful answer to "how did 1st shift do", and it's why this
page needed no new entry query. A true as-of-that-moment view is possible
(entries is append-only, so `created_at <= <timestamp>` would do it) but is a
different feature and isn't built here.

Access model matches /dashboard and /console: no auth, URL obscurity only,
per the architecture doc. This router is read-only — it has no write path of
any kind, which is what makes it safe to leave open alongside the others.
"""
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db.entries import (
    get_available_entry_dates,
    get_latest_entries_for_date,
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
    """The zone sections to render, top to bottom, per SUPERVISOR_ZONE_ORDER.

    Unlike /dashboard's zone pages, which each render one zone, this page
    stacks all of them — a supervisor reviewing the day is on a computer, not
    a fixed floor display, and wants the whole floor in one scroll.
    """
    zones = []
    for slug in SUPERVISOR_ZONE_ORDER:
        machine_ids = DASHBOARD_ZONES.get(slug)
        if machine_ids is None:
            continue  # slug retired from DASHBOARD_ZONES — skip, don't raise
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

    `date` and `shift` are optional query params so one particular view can be
    bookmarked or pasted to someone else; supervisor.js keeps them in sync
    with the on-page controls via history.replaceState. Neither is validated
    into an error here — the shell renders identically either way, and
    /api/supervisor-data resolves a junk date to a sensible one rather than
    500ing, the same forgiving approach console_batch_page takes with its own
    date box.
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
            # ALL_DAY_LABEL isn't a real shift, so it has to bypass
            # resolve_shift() — which would reject it and fall back to the
            # current clock shift, quietly ignoring a bookmarked All Day view.
            "requested_shift": (
                ALL_DAY_LABEL if shift == ALL_DAY_LABEL else resolve_shift(shift, now)
            ),
        },
    )


@router.get("/api/supervisor-data")
def supervisor_data(date: str | None = None):
    """Everything /supervisor needs for one production date, in one response.

    Returns the whole day — all 12 time slots — rather than one shift's four,
    plus a per-shift operator/issue map. That's deliberate: the shift toggle
    then switches columns entirely client-side with no round trip, which is
    what makes it feel instant. The payload is small enough to afford it
    (34 machines x 12 slots caps out at 408 rows).

    Date resolution is forgiving in one direction only. A missing or
    unparseable date falls back to the most recent date that has entries. But
    a date that parses fine and simply has no entries is honoured as-is and
    comes back with an empty `entries` list — bouncing someone somewhere else
    when they deliberately picked a quiet Sunday would be more confusing than
    showing them the empty day they asked for.
    """
    available = get_available_entry_dates()
    requested = _parse_date(date)

    if requested is None:
        resolved = available[0] if available else datetime.now().date()
    else:
        resolved = requested

    entries = get_latest_entries_for_date(resolved)

    # One get_shift_activity() call per shift — six queries per load. The
    # dashboard only ever needs the shift in progress, but a review page can
    # be looking at any of the three, and the toggle is client-side so all
    # three have to be in hand up front. Affordable here precisely because
    # this page doesn't poll: it loads a handful of times a day, versus the
    # dashboard's every-60-seconds. Keeping get_shift_activity() untouched
    # also means the operator/issue carry-forward rule stays defined in
    # exactly one place rather than being reimplemented per-shift here.
    shift_activity = {
        label: get_shift_activity(resolved, SHIFT_SLOTS[label]) for label in SHIFT_ORDER
    }

    return {
        "date": resolved.isoformat(),
        "available_dates": [d.isoformat() for d in available],
        "has_any_data": bool(available),
        "entries": entries,
        "shift_activity": shift_activity,
    }
