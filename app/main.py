"""track.fox.com entrypoint.

One FastAPI app serves both the JSON API and the server-rendered pages.
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.routers import console, console_oee, dashboard, oee, supervisor

app = FastAPI(title="track.fox.com")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)

# Order matters: console_oee owns /console/oee and console owns /console/{zone}.
# FastAPI matches in registration order, so registering console first would
# treat "oee" as a zone name and return a 404.
app.include_router(console_oee.router)
app.include_router(console.router)

app.include_router(supervisor.router)
app.include_router(oee.router)


@app.get("/")
def root():
    return RedirectResponse(url="/dashboard")


@app.get("/healthz")
def healthz():
    """Liveness check. Doesn't touch the database."""
    return {"status": "ok"}
