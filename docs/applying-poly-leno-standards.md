# Applying the Poly and Leno standards

## What this does and why

Until now, the Poly machines (`P1`-`P4`) and Leno machines (`AS1`-`AS7`) had
either no rows in the `standards` table or placeholder rows with
`standard_units = 0`. Both cases are broken, in different ways:

- **No row** — `/console` refuses the entry outright with
  `StandardNotFoundError` and marks that machine's row *failed*.
- **A row with 0** — worse, because it looks like it works. The entry saves,
  but `compute_status()` in `app/db/entries.py` is
  `units_produced >= standard_units`, so *any* number clears a bar of zero.
  The cell is permanently green and can never turn red.

This runbook replaces those rows with the real numbers from the floor's
standards spreadsheet, and retires the old `A1`-`A7` machine IDs that an
earlier revision used for Leno.

**Two scripts, applied in a specific order relative to a code deploy:**

| Script | What it does |
| --- | --- |
| `docs/sql/04_seed_new_machines_standards.sql` | Real standards for `P1`-`P4` and `AS1`-`AS7` (132 rows) |
| `docs/sql/05_drop_legacy_leno_machine_ids.sql` | Deletes the orphaned `A1`-`A7` rows |

## Before you start

- SSH access with `sudo` to the Postgres VM (`POSTGRES_HOST` in `.env`,
  currently `192.168.20.25`). This is the standalone Postgres server, not
  lost-woods.
- **Run these as the `postgres` superuser, not as `trackfox_app`.** Per
  `docs/sql/02_schema.sql`, the app's role has only `GRANT SELECT ON
  standards` — deliberately, since standards are maintained by seed script
  and never by an app endpoint. Connecting as `trackfox_app` to run these
  will fail with a permission error. That is the schema working correctly,
  not something to fix by widening the grant.
- Nothing here goes through CI/CD. Database changes are always manual on
  this project.

---

## Step 1 — Copy the scripts onto the Postgres server

From your workstation, at the repo root:

```bash
scp docs/sql/04_seed_new_machines_standards.sql docs/sql/05_drop_legacy_leno_machine_ids.sql youruser@192.168.20.25:/tmp/
```

## Step 2 — Back up the standards table

Small table, instant to dump, and it makes the rollback below trivial. Do not
skip it.

```bash
sudo -u postgres pg_dump -d trackfox -t standards --data-only -f /tmp/standards_backup_$(date +%F).sql
```

## Step 3 — Record the "before" state

Write these numbers down; Step 5 compares against them.

```bash
sudo -u postgres psql -d trackfox -c "SELECT count(*) AS total_rows FROM standards;"
```

```bash
sudo -u postgres psql -d trackfox -c "SELECT machine_id, count(*) AS slots, min(standard_units) AS min_std FROM standards GROUP BY machine_id HAVING min(standard_units) = 0 ORDER BY machine_id;"
```

The second query lists every machine currently stuck permanently green. You
should see `P1`-`P4` and `A1`-`A7` if the old placeholder was ever applied,
or no rows at all if it was not. Either is fine — both are handled.

## Step 4 — Apply the real standards

```bash
sudo -u postgres psql -d trackfox -f /tmp/04_seed_new_machines_standards.sql
```

The script is `ON CONFLICT ... DO UPDATE`, so it inserts what is missing and
overwrites what is already there. Safe to re-run any time a standard changes.

Expected output: `INSERT 0 132`.

## Step 5 — Verify the standards landed

```bash
sudo -u postgres psql -d trackfox -c "SELECT machine_id, count(*) AS slots, min(standard_units) AS min_std, max(standard_units) AS max_std FROM standards WHERE machine_id ~ '^(P[1-4]|AS[1-7])$' GROUP BY machine_id ORDER BY machine_id;"
```

Expect exactly 11 rows, every one showing `slots = 12` and a `min_std` well
above zero:

| machine_id | slots | min_std | max_std |
| --- | --- | --- | --- |
| AS1–AS5 | 12 | 2520 | 10080 |
| AS6–AS7 | 12 | 2250 | 9000 |
| P1 | 12 | 14400 | 57600 |
| P2–P4 | 12 | 16200 | 64800 |

Then confirm nothing anywhere is still zeroed *except* the legacy `A1`-`A7`
rows, which Step 7 removes:

```bash
sudo -u postgres psql -d trackfox -c "SELECT machine_id, count(*) FROM standards WHERE standard_units = 0 GROUP BY machine_id ORDER BY machine_id;"
```

**Poly is fixed as of this step** — `P1`-`P4` are unchanged IDs, so the
running app picks up the new standards immediately with no deploy. Any entry
logged from here on compares properly and can go red. Leno still needs the
deploy below.

## Step 6 — Deploy the code change

`app/models.py` now lists Leno as `AS1`-`AS7` instead of `A1`-`A7`. Push to
`main` and let the normal pipeline run (GitHub runner builds → GHCR →
self-hosted `track-fox` runner pulls and restarts the `app` container).

Confirm the container actually came up on the new image before continuing:

```bash
sudo docker compose -f /opt/track-fox-com/docker-compose.yml ps
```

Then load `/dashboard/leno` and check the grid renders seven rows labelled
`AS1` through `AS7`.

> **Order matters.** Do not run Step 7 before this deploy finishes. Until the
> new image is live, the running app still asks for `A1`-`A7`, and deleting
> those rows first would make Leno entries fail with `StandardNotFoundError`
> in the gap between the two.

## Step 7 — Retire the legacy `A1`-`A7` rows

The script leads with a `SELECT` so you can see what it is about to touch.
Run it and read that output first:

```bash
sudo -u postgres psql -d trackfox -f /tmp/05_drop_legacy_leno_machine_ids.sql
```

If the `SELECT` shows rows in the `entries` table under `A1`-`A7`, someone
logged real production against the old IDs. The script deliberately does
**not** touch those — `entries` is append-only by design. It leaves you a
commented-out `UPDATE` to rename them, plus an explanation of the tradeoff,
to run by hand only if you decide you want that history back on the Leno
dashboard.

## Step 8 — Final verification

```bash
sudo -u postgres psql -d trackfox -c "SELECT count(*) AS total_rows FROM standards;"
```

Expect **408** — 34 machines x 12 slots. This is the same number whether or
not the old placeholder had ever been applied, because Step 7 removes the
extra `A1`-`A7` rows either way.

```bash
sudo -u postgres psql -d trackfox -c "SELECT count(*) AS should_be_zero FROM standards WHERE standard_units = 0;"
```

Expect **0**. Any machine still listed here is still permanently green.

Last, the real test: open `/console/b4`, log a deliberately low number for a
Poly machine, and confirm the cell on `/dashboard/b4` comes up **red**. That
is the behaviour that was impossible before.

---

## Rollback

If Step 4 or 7 goes wrong, restore from the Step 2 dump:

```bash
sudo -u postgres psql -d trackfox -c "TRUNCATE standards;"
```

```bash
sudo -u postgres psql -d trackfox -f /tmp/standards_backup_<date>.sql
```

`standards` is pure reference data with no foreign keys pointing at it, so a
truncate-and-reload is safe and does not touch a single row of `entries`.

To roll the code back, revert the `app/models.py` commit and let CI/CD
redeploy — but note that if you have already run Step 7, the `A1`-`A7`
standards are gone, so a reverted app would fail on Leno entries until you
re-ran the old placeholder. In practice, roll forward rather than back.

---

## One thing this does not fix

`status` is computed once at write time and stored on the row, not
recalculated when the dashboard reads it. That was a deliberate design
decision (snapshot the verdict, keep the history honest), and it means **every
Poly entry saved while the standard was 0 keeps its bogus `:)` forever.**
Those cells stay green no matter what you do here.

Correct comparison starts with the next entry logged after Step 4. If the
floor wants past Poly days to read accurately,
`05_drop_legacy_leno_machine_ids.sql` carries a commented-out recompute query
for exactly that — but it rewrites history in place rather than appending to
it, so it is off by default and should be a conscious choice.

## If you use pgAdmin instead

Same scripts, same order. Open the Query Tool against `trackfox` **connected
as `postgres`, not `trackfox_app`**, then paste and run each file's contents.
Remember to disable SSL mode in the connection settings if this server still
has no SSL configured.
