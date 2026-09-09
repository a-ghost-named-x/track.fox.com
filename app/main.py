"""track.fox.com v2 — entrypoint.

Single Python app serving both the API and the server-rendered frontend,
per the architecture decision (no separate frontend service).
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routers import console, console_oee, dashboard, oee, supervisor

app = FastAPI(title="track.fox.com")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)

# ORDER MATTERS: console_oee owns the literal path /console/oee, while console
# owns /console/{zone}. FastAPI matches in registration order, so registering
# console first would capture /console/oee as a zone named "oee" and 404 it.
# The failure is silent and looks exactly like a missing page.
app.include_router(console_oee.router)
app.include_router(console.router)

app.include_router(supervisor.router)
app.include_router(oee.router)


@app.get("/")
def root():
    # No real landing page yet — /dashboard is the primary surface.
    return RedirectResponse(url="/dashboard")


@app.get("/healthz")
def healthz():
    """Liveness check — does not verify DB connectivity, just that the app is up."""
    return {"status": "ok"}
