"""Routes for /dashboard — the public, no-auth display BrightSign renders.

Example here is a "mixed" dashboard per the architecture doc: it merges a
read-only MSSQL query with manual-entry data from Postgres into one view.
Swap or duplicate this pattern for SQL-only or manual-entry-only dashboards.
"""
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.entries import get_latest_entries_for_date
from app.models import MACHINE_IDS, TIME_SLOTS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    """Initial page load — renders the shell; data is filled in via polling."""
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "machine_ids": MACHINE_IDS,
            "time_slots": TIME_SLOTS,
            "poll_interval_ms": settings.poll_interval_ms,
        },
    )


@router.get("/api/dashboard-data")
def dashboard_data():
    """JSON endpoint the dashboard page polls periodically (see static/js/dashboard.js).

    Returns today's manual entries, pivoted by the frontend into the grid.
    TODO: this is where a SQL-only or mixed dashboard would also query MSSQL
    via app.db.mssql.run_readonly_query(...) and merge results before
    returning — left out here since the production query itself depends on
    your actual MSSQL schema, which this scaffold doesn't have visibility into.
    """
    today = date.today()
    entries = get_latest_entries_for_date(today)
    return {"date": today.isoformat(), "entries": entries}
