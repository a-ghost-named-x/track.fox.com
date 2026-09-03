"""track.fox.com v2 — entrypoint.

Single Python app serving both the API and the server-rendered frontend,
per the architecture decision (no separate frontend service).
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routers import console, dashboard, oee, supervisor

app = FastAPI(title="track.fox.com")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)
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
