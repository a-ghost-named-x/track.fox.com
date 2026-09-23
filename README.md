# track.fox.com

A production tracking web app for a manufacturing floor. It replaced a set of
hand-maintained Excel/SharePoint dashboards that were shown on the floor's
digital signage screens.

Every two hours someone walks the floor and records each machine's counter.
The app turns those readings into live green/red boards for the floor, a
shift-by-shift review page for supervisors, and an OEE (Overall Equipment
Effectiveness) report with a downtime Pareto.

It covers 34 machines in five zones, three shifts a day.

## What's in it

| Page | Who it's for | What it does |
|---|---|---|
| `/dashboard/<zone>` | The floor screens | One grid per zone. Each cell is a machine's reading at a 2-hour checkpoint, green if it met its standard and red if not. Polls for new data and switches shifts on its own. |
| `/console/<zone>` | The 2-hour rounds | Enter every machine in a zone on one form. |
| `/console` | Corrections | One machine at a time. |
| `/console/oee` | End of shift | One person enters the whole floor's downtime, scrap, scheduling and shift lengths. |
| `/supervisor` | Supervisors | Any past day and shift, including machines that never reported. |
| `/oee` | Supervisors | OEE per machine per shift, zone and floor rollups, and a downtime Pareto over a shift, 7 days or 30 days. |
| `/dashboard` | Everyone | Site menu. |

[docs/how-the-numbers-work.md](docs/how-the-numbers-work.md) explains every
number on the site in plain language, without code.

## How it's built

- **One FastAPI app** serves both the JSON API and the server-rendered pages
  (Jinja2 templates plus plain JavaScript). No frontend build step.
- **Postgres** holds the manually entered data. The app connects as a
  low-privilege role.
- **MSSQL** (read-only) is where automated production data will come from.
  The connection helper exists; the query is still a TODO.
- **Docker Compose** on a single Linux VM, behind a separate Traefik instance.
- **CI/CD:** a push to `main` builds the image on GitHub Actions, pushes it to
  GHCR, and a self-hosted runner on the server pulls it and restarts the
  container. Database changes are applied by hand.

### Design decisions worth knowing about

- **Entries are append-only.** A correction is a new row, and readers take the
  newest row per machine and slot. Nothing is overwritten, so the history is
  always there.
- **Status is computed at write time** by comparing the reading against that
  machine's standard for that slot, and stored on the row. Changing a
  standard later doesn't recolour old cells.
- **A blank checkpoint means "unchanged", not "unknown".** The floor leaves a
  box empty when the count hasn't moved. Treating blanks as unknown would
  quietly drop a machine's worst hours and inflate its score.
- **Missing data is never defaulted.** No downtime entry doesn't mean zero
  downtime, and no scrap entry doesn't mean perfect quality. The page shows
  "—" and says what's missing.
- **OEE data lives in its own tables.** The grid resolves each cell by
  "newest row wins", so a scrap entry on the same table would blank out the
  production number entered before it.
- **Rollups never average percentages.** They sum units and minutes, then
  divide once. Availability is weighted by each machine's capacity, because a
  minute on a slow machine isn't worth a minute on a fast one. The tests
  check that A × P × Q equals OEE at both machine and zone level.
- **No authentication, on purpose.** Access is by link only, and the app is
  meant for an internal network. Don't expose it to the internet as-is.

## Project layout

```
app/
  main.py            FastAPI app and router registration
  config.py          settings from environment / .env
  models.py          machine roster, zones, shifts, shift-length rules, request models
  db/
    entries.py       2-hour production entries
    oee.py           OEE tables and the OEE / Pareto computation
    postgres.py      Postgres connection
    mssql.py         read-only MSSQL connection
  routers/           one module per page group
  templates/         Jinja2 templates
  static/            CSS and JavaScript
tests/               plain-Python tests, no framework
docs/                how-the-numbers-work.md
```

## Running it locally

You need Docker and a Postgres server the app can reach.

1. Copy `.env.example` to `.env` and fill in the Postgres connection.
2. Start it:

   ```bash
   docker compose up --build
   ```

   `docker-compose.override.yml` is picked up automatically for local
   development. It builds from the local Dockerfile, mounts `./app` with
   auto-reload, and serves on http://localhost:8000.

### Database

The SQL scripts used to create and seed the database aren't included in this
repository. The app expects these tables:

- `entries`: the 2-hour readings (append-only)
- `standards`: target units per machine per time slot (cumulative within a
  shift)
- `machine_ideal_rates`: each machine's theoretical maximum, units per hour
- `downtime_reasons`: reason codes, with `is_planned` and `active` flags
- `shift_downtime_entry` and `shift_downtime_reason`: one downtime submission
  per machine per shift, plus its reasons
- `shift_scrap`: scrap per machine per shift
- `machine_schedule`: scheduling exceptions
- `machine_shift_length`: non-default shift lengths

The column names can be read from the queries in `app/db/`.

## Tests

The tests stub out the database, so none of them needs one.

```bash
python tests/test_oee_math.py
```

```bash
python tests/test_oee_report.py
```

The route smoke test also needs `httpx` (in `requirements-dev.txt`) and must be
run from the repo root:

```bash
python tests/test_routes_smoke.py
```
