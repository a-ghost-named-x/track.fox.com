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
- Standards are **not** uniform across machines, and not even within a group — there are eight distinct per-slot increments across the 34 machines: C1–C5/C14–C16 = 11,700; C6–C11 = 10,800; WS = 9,900; FM = 8,100; P1 = 14,400; P2–P4 = 16,200; AS1–AS5 = 2,520; AS6–AS7 = 2,250. (An earlier revision of this file claimed the original 23 shared one set of targets; they don't.) Within any machine, its four per-shift checkpoints repeat identically across all three shifts, ramping 1x/2x/3x/4x off the increment.
- A standard is **75% of the machine's theoretical maximum** (confirmed with the floor 2026-09-03). That factor is what the OEE feature's ideal rates are derived from — see `machine_ideal_rates` below.
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
- `/supervisor` is the historical shift-review page (added 2026-09-01): pick a production date, then a shift, and see that shift's grid after the fact. Read-only, no auth, same access model as the other surfaces. Linked from the `/dashboard` zone-picker index only — never from `/dashboard/<zone>`, which are the BrightSign boards and carry no navigation. All five zones stack on one page in `SUPERVISOR_ZONE_ORDER` (`app/models.py`), Combo/FMW first. The shift toggle has a fourth "All Day" option (`ALL_DAY_LABEL`) that unhides all 12 slot columns at once; operator and issue are hidden there, since `get_shift_activity()` is scoped to one shift's four slots and three shifts' worth of values can't share a cell.
- `/supervisor` deliberately differs from `/dashboard` in three ways, all documented in `app/routers/supervisor.py`'s module docstring: it doesn't poll (a fixed past date has nothing to poll for), it shows every machine including ones that never reported (a blank row is the finding, not clutter — the opposite of `applyMachineVisibility()` in `dashboard.js`), and it opens on the most recent date *with data* rather than today.
- The numbers `/supervisor` shows are latest-per-slot, **corrections included** — it reuses `get_latest_entries_for_date()` untouched, so it is not a frozen photograph of what the board displayed at 2PM. A true as-of-that-moment view is possible (`entries` is append-only, so `created_at <= <timestamp>` would do it) but is a separate feature and was not built.
- The Issue column shows one line per slot that reported an issue, prefixed with the slot it was logged against (`10AM  belt slip`), instead of a single unlabelled string. `get_shift_activity()` returns an `issues` list (`DISTINCT ON (machine_id, time_slot)`) rather than one `issue` — before this it took only the newest issue in the shift and dropped every earlier one. Consecutive slots reporting the same text collapse into a range (`8AM-10AM  defective material`), so carry-forward doesn't stack near-identical lines. Rendering is shared by both grids in `app/static/js/issue_cell.js`; `/dashboard` caps at `MAX_ISSUE_LINES` (3) with a `+N earlier` line, `/supervisor` shows all. A white corner flag on the slot cell itself (`.cell[data-has-issue]`) ties a line back to the column it happened in — white because a reporting cell is already filled solid green or red.

- `/oee` is the Overall Equipment Effectiveness page (added 2026-09-03): OEE = Availability x Performance x Quality, per machine per shift, plus a downtime Pareto. Follows `/supervisor`'s conventions (no polling, shows every machine, opens on the most recent date *with data*) and is linked from the `/dashboard` zone-picker index, never from the boards. Read-only. Three shifts only — no "All Day", since each shift has its own Planned Production Time, downtime and crew, and one number across all three would average away the point. Math and queries live in `app/db/oee.py`; formulas and the worked example are in its module docstring and in `docs/sql/08_seed_ideal_rates.sql`.
- OEE's data lives in **five new tables**, deliberately NOT as columns on `entries`: `slot_scrap`, `slot_downtime_entry` + `slot_downtime_reason`, `machine_schedule`, `machine_ideal_rates`, plus the `downtime_reasons` lookup (`docs/sql/06_oee_schema.sql`). Nothing about `entries` or `standards` was altered. Deployment procedure: `docs/deploying-oee.md`.
- `/console/<zone>`'s batch form grew three column groups — Scrap (cumulative), Downtime (a "none" checkbox plus up to `MAX_DOWNTIME_REASONS` reason/minutes pairs), and a per-shift Scheduled checkbox. Each saves **independently**; a blank field means "not touching this", never "set to zero".
- Tests live in `tests/`, runnable directly with no framework (`python tests/test_oee_math.py`). `test_routes_smoke.py` additionally needs `httpx` from `requirements-dev.txt`. None of `tests/` enters the Docker image — the Dockerfile only copies `requirements.txt` and `app/`.
- `/supervisor`'s date picker is clamped to the real first/last day of data via `get_available_entry_dates()` (`SELECT DISTINCT entry_date`, riding `idx_entries_date`), so an impossible date can't be picked. Interior gaps (a quiet Sunday) stay selectable — an HTML date input can clamp a range's ends but not disable dates inside it — and render an explicit "no entries recorded" state instead of a dead grid.

## On the horizon

- Confirm whether the 75%-of-theoretical figure is a *measured* nameplate rate or a rule of thumb applied when the targets were set. Everything on `/oee` is scaled by it. `docs/sql/09_check_ideal_rate_calibration.sql` tests it against real history: if no machine's best-ever 2-hour slot approaches its derived ceiling, the ceiling is probably wrong and 85% OEE is unreachable by construction. Overrides go in `machine_ideal_rates`, which is keyed per machine for exactly that reason.
- Cross-validating manual counts against the machine lines' own total count is **deliberately not** a website feature — the data is captured (`good + scrap`) so someone can do it by hand when they want to. Don't build tolerance checks for it unasked.

- Implement the MSSQL read query in the dashboard router (blocked on confirming the real MSSQL schema).
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
- `trackfox_app` has only `GRANT SELECT ON standards` by design — seed scripts must be run as the `postgres` superuser, not the app role. Same for `downtime_reasons` and `machine_ideal_rates`.
- **Never put a second writer's column on `entries`.** `get_latest_entries_for_date()` resolves the grid with `DISTINCT ON (machine_id, time_slot) ORDER BY created_at DESC` — the newest **row** wins wholesale. Scrap on `entries` would mean the scrap person's submission becomes the newest row for that cell and blanks the units the production person entered minutes earlier, taking the BrightSign boards down. That single fact is why OEE got its own tables.
- **A downtime submission with zero reasons is not the same as no submission.** The first says "ran clean, 100% availability"; the second says "nobody has entered this yet". Collapsing them would default missing data to a perfect score — the `standard_units = 0` trap one layer up. That's why downtime is a header table plus children rather than a nullable column pair.
- **Rolling OEE up across machines requires a capacity-weighted Availability**, not `run_minutes / ppt_minutes`. With 8 different ideal rates on the floor, clock-time availability makes a zone's `A x P x Q` multiply out to something that isn't its OEE, because a minute on AS1 (1,680/hr) isn't worth a minute on C1 (7,800/hr). For a single machine the two definitions are identical. `tests/test_oee_math.py` asserts the identity at both levels — it's what caught this.
- **Pydantic's `ValidationError` is a subclass of `ValueError`.** A bare `except ValueError` placed *before* `except ValidationError` silently swallows validation failures and mislabels them as bad number formatting. Order the specific case first (see the write paths in `app/routers/console.py`).
- **An unticked checkbox and an absent one are indistinguishable in a form POST** — neither appears in the body. For the Scheduled checkbox that ambiguity was dangerous: reading absence as "unticked" marked every machine not on the submitted form as not-scheduled, removing them from the OEE denominator and inflating every number. Fixed with a hidden `sched_present_<machine>` companion field the browser always posts.
- OEE is computable from **good units + downtime alone** — scrap cancels out of `A x P x Q`. So a slot missing its scrap still yields a real OEE with Performance and Quality as N/A, rather than being discarded. Don't "simplify" that into all-or-nothing.

## Working style notes

- Not a professional software developer — prefers explanations in plain terms, not just code dumped without context.
- Prefers incremental, file-by-file codebase exploration — understand each piece before moving on.
- Prefers step-by-step confirmation before proceeding on infra/tooling changes.
- Prefers clarifying requirements thoroughly before code gets written.
- Architectural decisions above are deliberate tradeoffs (no auth by design, separate Postgres from MSSQL, append-only entries) — don't relitigate them without a specific reason; treat them as constraints, not open questions.

## Stack reference

FastAPI, Uvicorn, Jinja2, Pydantic, pyodbc (MSSQL), asyncpg/psycopg (Postgres). Docker Compose, GHCR, GitHub Actions, pgAdmin.
