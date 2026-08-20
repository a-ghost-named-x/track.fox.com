# track.fox.com v2 — Project Context

Factory floor production tracking web app for a manufacturing environment, replacing manually maintained Excel/SharePoint dashboards shown on BrightSign digital signage. Power BI was evaluated and rejected (on-prem data gateway complexity + OAuth token-refresh incompatibility with BrightSign). Owner is a solo IT/infrastructure admin, not a software developer by trade.

## Architecture (locked in)

- Single Python/FastAPI app serves both the backend API and server-rendered frontend (Jinja2 templates + JS polling/charts) — no separate frontend service.
- Deployed via Docker Compose on a single Ubuntu VM, hostname **lost-woods**. No k3s for this project (unlike chat.fox.com / track.fox.com v1).
- Two containers: `app` + `postgres:16-alpine`. Postgres has no exposed host port in production — reachable only over the internal Docker bridge network.
- App connects to Postgres via a dedicated low-privilege role (`trackfox_app`), never superuser.
- Read-only SELECT queries against an existing **MSSQL server** for automated production data — kept deliberately separate from Postgres to avoid load/write-risk on that system. No writes to MSSQL, ever.
- **lost-woods is a standalone server** — never reference its services via `localhost`; always use hostname or IP.

## Data model (locked in)

- `entries` table: `machine_id, operator, time_slot, units_produced, status, issue, entered_by, entry_date, created_at`. Append-only / history-preserving — never overwrite in place.
- `status` (`:)` / `:(`) is computed at write-time by comparing `units_produced` against a per-machine, per-time-slot cumulative standard in a `standards` reference table.
- 12 fixed time slots: 8AM, 10AM, 12PM, 2PM, 4PM, 6PM, 8PM, 10PM, 12AM, 2AM, 4AM, 6AM. Units are cumulative across each 8-hour shift window.
- No real authentication — link-based access only: `/dashboard` (public display) and `/console` (entry form, URL-obscurity only, not credentials).
- `entered_by` is a free-text employee number — format-validated but not verified against HR/AD.
- Machines: 34 total, grouped into five dashboard zones (`DASHBOARD_ZONES` in `app/models.py`): `b3`/Combo-FMW (C1–C11, C14–C16), `b2`/FM (FM1–FM3), `ws`/WS (WS1–WS6), `b4`/Poly (P1–P4), `leno`/Leno (AS1–AS7). Leno is `AS1`–`AS7`, not `A1`–`A7` — an earlier revision used the short form and those rows were retired in `docs/sql/05_drop_legacy_leno_machine_ids.sql`.
- Standards are **not** uniform across machines. The original 23 (C/FM/WS) share one set of cumulative targets; Poly and Leno each have their own. Within any machine, its four per-shift checkpoints repeat identically across all three shifts.
- Dashboards come in three flavors: SQL-only (MSSQL-driven), manual-entry-only (no SQL dependency, updated ~every 2 hours), and mixed (API layer merges both sources so the display layer doesn't care which field came from where).

## CI/CD

Push to `main` → GitHub-hosted runner builds Docker image → pushes to GHCR → self-hosted runner (label `track-fox`, running as unprivileged user `ghrunner` on lost-woods) pulls the image and restarts the `app` container via `docker compose` from `/opt/track-fox-com/`.

- GitHub org: `a-ghost-named-x`, repo: `track.fox.com`.
- Database changes are **not** part of the CI/CD pipeline — they're applied manually via SSH + pgAdmin/psql directly on lost-woods.
- Reference pattern established by existing internal tools: chat.fox.com and track.fox.com v1 (GitHub Actions → GHCR → Docker).

## Current state (as of 2026-08-20)

- CI/CD pipeline working end-to-end.
- Postgres fully scaffolded: `standards` and `entries` tables created, seed data loaded for all 34 machines × 12 slots = **408 rows** (`docs/sql/03_seed_standards.sql` for the original 23, `04_seed_new_machines_standards.sql` for Poly + Leno).
- FastAPI app live on lost-woods, reachable via IP at `/dashboard` and `/console`.
- `/api/dashboard-data` was already querying live Postgres entries via `get_latest_entries_for_date()` — the missing piece was that `app/models.py`'s `MACHINE_IDS` (was `["C1", "C2"]`) and `TIME_SLOTS` (wrong order) didn't match the real seeded roster, so most machines had no grid row to render into. Fixed: `MACHINE_IDS` was filled out with all 23 machines then on the roster (C1–C11, C14–C16, FM1–FM3, WS1–WS6), `TIME_SLOTS` reordered to start at 8AM. Poly and Leno were added later, bringing it to 34.
- Added a dynamic shift indicator to `/dashboard`: `SHIFTS` in `app/models.py` defines three 8-hour windows (1st: 6AM–2PM, 2nd: 2PM–10PM, 3rd: 10PM–6AM); `get_current_shift()` in `app/routers/dashboard.py` computes the current one from server-local time and returns it in the `/api/dashboard-data` payload; `dashboard.js`/`dashboard.html` render it next to the date. Recomputed on every poll, so it updates within one `POLL_INTERVAL_MS` of a shift boundary. Display-only — doesn't affect `time_slot`/`standards` logic.
- `/console/<zone>` has a shift toggle (1st/2nd/3rd) and an editable date box; `/console` got the date box only. Before this, the batch form derived its shift from `get_current_shift()` — the same display-delayed function the dashboard uses — so the 3PM changeover took 1st Shift's time slots out of its dropdown and locked people out of entering 1st Shift numbers they often don't finish collecting until 4PM. `resolve_shift()` in `app/models.py` now takes the selected shift (query param on GET, hidden field on POST) and only falls back to `get_current_shift()` when nothing valid is supplied, so the console's choice is independent of the dashboard's display rule while defaults stay unchanged. The dashboard still follows the clock — deliberately untouched.
- The date box exists because `entry_date` was hardcoded to `date.today()` on both forms. It matters for 3rd Shift (whose 12AM–6AM slots fall on the calendar day *after* the shift starts) and for next-morning corrections. `entry_date` is what `/dashboard` filters on, so that box decides which day's grid an entry lands on.
- `/console/<zone>` POST rejects a time_slot that isn't in the selected shift's `SHIFT_SLOTS` — the two come from separate controls, and a mismatch would file real numbers under a slot nobody looks at on that shift's grid.
- Poly (`P1`–`P4`) and Leno (`AS1`–`AS7`) standards are seeded with real numbers as of 2026-08-20. They had been placeholder rows of `standard_units = 0`, which made every Poly/Leno cell permanently green — see the gotcha below. Leno was renamed from `A1`–`A7` in the same pass. Procedure and verification queries: `docs/applying-poly-leno-standards.md`.
- A `TODO` in `dashboard.py` still marks where MSSQL querying will eventually be merged into the mixed-dashboard response.
- Historical data display (beyond "latest entry per machine/slot for today") is intentionally deferred — not a current priority.

## On the horizon

- Implement the MSSQL read query in the dashboard router (blocked on confirming the real MSSQL schema).
- Historical data view/display — deferred, not yet scoped.
- Finalize firewall/port configuration on lost-woods (VM currently on a secured VLAN; deferred).
- Complete the mixed-dashboard pattern (SQL + manual entry combined) per the architecture doc.
- Finalize exact columns for the manual-entry dashboard(s) and decide whether one shared form feeds multiple dashboards or each dashboard gets its own form.

## Key gotchas learned the hard way

- MSSQL ODBC driver belongs only in the `app` container's Dockerfile — irrelevant to the Postgres `db` container.
- `docker-entrypoint-initdb.d` scripts only fire on a fresh volume; schema/seed SQL must be applied manually against an existing volume.
- `docker compose up -d` won't recreate unchanged containers — use `--force-recreate` (e.g. `sudo docker compose up -d --force-recreate db`).
- `python:3.12-slim` silently rolled to Debian 13 (trixie) and broke the MSSQL ODBC driver install. Pinned to `python:3.12-slim-bookworm` instead (Microsoft's Debian 13 signing key gap is an unresolved upstream issue).
- `docker/build-push-action` cache requires `docker/setup-buildx-action@v3` (switches from the `docker` driver to `docker-container` driver) for cache export to work.
- Self-hosted runner runs as a dedicated unprivileged account (`ghrunner`), not a personal admin account.
- pgAdmin: disable SSL mode when connecting to a Postgres container that has no SSL configured, or you'll get connection errors.
- A `standards` row of `standard_units = 0` is far more dangerous than a *missing* row. Missing raises `StandardNotFoundError` and `/console` visibly marks the row failed; zero saves silently and, because `compute_status()` is `units_produced >= standard_units`, makes the cell permanently green. Placeholder seed files must never be applied with their zeros still in them.
- `trackfox_app` has only `GRANT SELECT ON standards` by design — seed scripts must be run as the `postgres` superuser, not the app role.

## Working style notes

- Not a professional software developer — prefers explanations in plain terms, not just code dumped without context.
- Prefers incremental, file-by-file codebase exploration — understand each piece before moving on.
- Prefers step-by-step confirmation before proceeding on infra/tooling changes.
- Prefers clarifying requirements thoroughly before code gets written.
- Architectural decisions above are deliberate tradeoffs (no auth by design, separate Postgres from MSSQL, append-only entries) — don't relitigate them without a specific reason; treat them as constraints, not open questions.

## Stack reference

FastAPI, Uvicorn, Jinja2, Pydantic, pyodbc (MSSQL), asyncpg/psycopg (Postgres). Docker Compose, GHCR, GitHub Actions, pgAdmin.
