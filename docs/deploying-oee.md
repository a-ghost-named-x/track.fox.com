# Deploying the OEE feature — step by step

Everything to do **before** and **after** `git push origin main`, in order.

Read the next section first. It is the only part of this that can cause an
outage, and it takes one command to avoid.

---

## ⚠ The ordering rule: database FIRST, push SECOND

Pushing to `main` triggers CI/CD, which builds the image and restarts the `app`
container automatically. The new code reads five tables that **do not exist
yet**. If the app deploys before the tables are created:

| Page | If you push before applying the SQL |
| --- | --- |
| `/console/<zone>` | **500 error.** It loads the downtime reason codes on page load. Nobody can enter production. |
| `/oee` | **500 error** on its data call. |
| `/console` (single-machine) | Fine — untouched. |
| `/dashboard/<zone>` | **Fine.** The floor screens don't touch any new table. |
| `/supervisor` | Fine — untouched. |

So the boards stay up either way, but the **entry form goes down**, which is
worse during a shift. Apply the SQL first and there is no window at all.

The good news: the migration is **purely additive**. It creates new tables and
alters nothing that already exists — no `ALTER TABLE` on `entries` or
`standards` anywhere. That means:

- The **currently running** app image is completely unaffected by the new
  tables. You can safely apply the SQL hours or days before you push.
- Rolling the app back later does **not** require rolling the database back.

---

## Step 1 — Run the tests locally

No database needed; they stub it out.

```bash
python tests/test_oee_math.py
```

```bash
python tests/test_oee_report.py
```

Both should end with `ALL CHECKS PASSED`. The third one additionally needs
`httpx` (it renders every page through FastAPI's test client):

```bash
python -m pip install -r requirements-dev.txt
```

```bash
python tests/test_routes_smoke.py
```

Should end with `ALL SMOKE CHECKS PASSED`. Run it **from the repo root** —
Jinja resolves `app/templates` relatively and won't find it from elsewhere.

> If `test_oee_math.py` fails on the check named
> `rollup A x P x Q == oee`, stop. That assertion is the one that catches
> broken aggregation, and a failure there means the numbers on `/oee` are
> wrong even though the page will render happily.

---

## Step 2 — Pre-flight the database (read-only, safe any time)

This proves the assumption every ideal rate is derived from, before anything
gets written. It only runs `SELECT`s.

The four queries live in the **PRE-FLIGHT CHECKS** section at the top of
`docs/sql/08_seed_ideal_rates.sql`. Two things about running them:

- **Run them one at a time**, not by executing the whole file. pgAdmin's query
  tool shows only the **last** result set when you run multiple statements, so
  executing the file would hide the very output you're here to read. Highlight
  one query, run it, look at it, move to the next.
- **Executing the whole file at this stage will stop with an error**, and that
  is expected and harmless — the seed at the bottom needs tables that
  `06_oee_schema.sql` hasn't created yet. There's a guard that says so in
  words. Everything above the guard is read-only, so nothing gets written and
  there's no half-applied state to clean up.

Expected results:

1. **Empty** — first slot of each shift agrees per machine
2. **Empty** — every shift ramps 1×/2×/3×/4×
3. **One row: `34 | 408`** — all machines, all slots
4. **Empty** — every ideal rate lands on a whole number

If check 2 or 3 comes back non-empty, **stop and fix `standards` first**. A
wrong standard becomes a wrong ideal rate becomes wrong OEE for every shift
from here on.

You can also run `docs/sql/09_check_ideal_rate_calibration.sql` now — also
read-only. Its third query (missing checkpoints) works against your existing
data today and will tell you whether "slots never get skipped" actually holds.

---

## Step 3 — Apply the three migrations

As the **`postgres` superuser**, not `trackfox_app`. The app role has
`SELECT`-only on reference tables by design, so it cannot run these — same
constraint as the existing standards seeds.

**Not through CI/CD.** Database changes have never been part of that pipeline
and still aren't.

Substitute your values from `.env` (`POSTGRES_HOST`, `POSTGRES_DB`). Use the
hostname or IP — never `localhost`; lost-woods is a standalone server.

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/06_oee_schema.sql
```

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/07_seed_downtime_reasons.sql
```

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/08_seed_ideal_rates.sql
```

`ON_ERROR_STOP=1` matters — without it psql plows through a failed statement
and you get a half-created schema with no obvious error.

**The order matters** — `06` creates the tables that `07` and `08` fill. Both
seed files check for their table up front and stop with an instruction if it
isn't there, so a wrong order costs you nothing but a re-run.

If you're using pgAdmin instead: remember to **disable SSL mode**, and open and
run the files in this order. All three are idempotent (`CREATE TABLE IF NOT
EXISTS`, `ON CONFLICT DO UPDATE`), so re-running any of them is safe — which
also means that if you hit the guard on `08` earlier, you just run `06`, `07`,
then `08` again and it picks up cleanly.

Note that `08` ends with two verification queries, and pgAdmin will show you
only the last one's output. Run those two individually afterwards (Step 4
repeats them) if you want to see both.

---

## Step 4 — Verify the schema

```sql
-- Expect all 6: downtime_reasons, machine_ideal_rates, machine_schedule,
-- slot_downtime_entry, slot_downtime_reason, slot_scrap
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_name IN (
  'downtime_reasons','slot_scrap','slot_downtime_entry',
  'slot_downtime_reason','machine_schedule','machine_ideal_rates')
ORDER BY table_name;
```

```sql
-- Expect 10 reason codes: 8 unplanned, 2 planned.
SELECT is_planned, count(*) FROM downtime_reasons GROUP BY is_planned;
```

```sql
-- Expect 34 rows. Spot-check: C1 = 7800.00, C6 = 7200.00, P2 = 10800.00,
-- AS1 = 1680.00, AS6 = 1500.00
SELECT machine_id, ideal_units_per_hour FROM machine_ideal_rates
WHERE machine_id IN ('C1','C6','P2','AS1','AS6') ORDER BY machine_id;
```

```sql
-- Every row should read exactly 75.00 — the anchor. If it doesn't, the
-- ideal rates and the standards disagree.
SELECT r.machine_id,
       round(100 * (s.standard_units / (r.ideal_units_per_hour * 2)), 2) AS oee_at_standard_pct
FROM machine_ideal_rates r
JOIN standards s ON s.machine_id = r.machine_id AND s.time_slot = '8AM'
ORDER BY r.machine_id;
```

Confirm the app role can read what it needs — run this **as `trackfox_app`**:

```sql
SELECT count(*) FROM downtime_reasons;
SELECT count(*) FROM machine_ideal_rates;
SELECT count(*) FROM slot_scrap;
```

All three must succeed. A `permission denied` here means the `GRANT`s at the
bottom of `06_oee_schema.sql` didn't apply, and `/console` will 500 after
deploy.

---

## Step 5 — Commit and push

You're on `main` and it's clean, so branch first:

```bash
git checkout -b feature/oee
```

Review what's changed before committing:

```bash
git status && git diff --stat
```

Expected — 7 modified, 11 new:

```
 modified:  CLAUDE.md
 modified:  app/main.py                       (registers the oee router)
 modified:  app/models.py                     (slot geometry + 3 payload models)
 modified:  app/routers/console.py            (4 independent write paths)
 modified:  app/static/css/style.css
 modified:  app/templates/console_batch.html  (scrap/downtime/scheduled columns)
 modified:  app/templates/dashboard_index.html (link to /oee)
 new:       app/db/oee.py                     (the OEE math)
 new:       app/routers/oee.py
 new:       app/templates/oee.html
 new:       app/static/js/oee.js
 new:       docs/sql/06_oee_schema.sql
 new:       docs/sql/07_seed_downtime_reasons.sql
 new:       docs/sql/08_seed_ideal_rates.sql
 new:       docs/sql/09_check_ideal_rate_calibration.sql
 new:       docs/deploying-oee.md
 new:       requirements-dev.txt
 new:       tests/
```

Nothing in `requirements.txt`, the `Dockerfile`, `docker-compose.yml`, or
`.env` changed — **no new dependencies and no new environment variables**. The
Dockerfile only copies `requirements.txt` and `app/`, so `tests/` and
`requirements-dev.txt` never enter the image.

```bash
git add -A && git commit -m "Add OEE tracking: downtime, scrap, scheduling and /oee dashboard"
```

Then merge to `main` and push — which is what triggers the deploy:

```bash
git checkout main && git merge --no-ff feature/oee && git push origin main
```

---

## Step 6 — Watch the deploy

```bash
gh run watch
```

Or the Actions tab on `a-ghost-named-x/track.fox.com`. Two stages: the
GitHub-hosted runner builds and pushes to GHCR, then the self-hosted runner
(label `track-fox`, user `ghrunner`) pulls and restarts from
`/opt/track-fox-com/`.

On lost-woods, confirm the container actually came back:

```bash
sudo docker compose -f /opt/track-fox-com/docker-compose.yml ps
```

```bash
sudo docker compose -f /opt/track-fox-com/docker-compose.yml logs --tail=50 app
```

---

## Step 7 — Post-deploy verification, in this order

**The boards first.** They're what the floor is looking at:

1. `http://<lost-woods>/dashboard/b3` — grid renders, numbers present, polling
   still updating. Nothing about this page changed; you're confirming you
   didn't break it.
2. `http://<lost-woods>/supervisor` — unchanged, still loads.

**Then the entry form** — this is the one the new code touches most:

3. `http://<lost-woods>/console/b3` — should show three new columns: **Scrap
   (cumulative)**, **Downtime**, and **Scheduled**. The reason dropdowns should
   be populated with all 10 codes. If this page 500s, it's almost certainly a
   missing `GRANT` — go back to Step 4.
4. Enter units for one machine as you normally would and save. Confirm it still
   appears on `/dashboard/b3`. **The existing workflow must be unchanged.**

**Then OEE:**

5. `http://<lost-woods>/oee` — will show mostly `—` at first, which is correct:
   OEE needs *both* units and a downtime entry for a slot, and no downtime has
   ever been entered. The completeness strip should read something like
   `Units 34/34  Downtime 0/34  Scrap 0/34`.

---

## Step 8 — Prove the write paths with one real machine

Pick one machine on the current shift and, on `/console/<zone>`:

1. Enter **scrap** only (leave units blank) → save. Confirm on `/dashboard`
   that the machine's **units did not change**. This is the single most
   important behaviour to verify: three people share this form, and a blank
   field must mean "not touching this" rather than "set to zero".
2. Tick **none** in the Downtime column → save. On `/oee`, that slot should now
   show a real OEE percentage with **Avail 100%**.
3. Enter a reason + minutes (say `MATL` / 20) → save. On `/oee`, Avail should
   drop and the reason should appear in the **Downtime by reason** Pareto at the
   bottom.
4. Untick **Scheduled** for one machine → save → confirm on `/oee` that the row
   dims and reads *not scheduled*, and that the zone rollup's machine count
   drops by one. Then re-tick it to undo.

Sanity anchor while you're looking at it: a machine that hit standard exactly
with no scrap and no downtime reads **75%**, not 100%. That's correct — see the
note at the top of `/oee`.

---

## Rollback

**App code** — revert and push; CI/CD redeploys the previous image:

```bash
git revert --no-commit HEAD && git commit -m "Revert OEE feature" && git push origin main
```

**Database** — you almost certainly don't need to. The migration adds tables
and changes nothing existing, so the reverted app runs against it perfectly
well, and any scrap/downtime already entered stays for when you re-deploy.

If you genuinely want it gone:

```sql
-- Destroys all scrap and downtime history. There is no undo.
DROP TABLE IF EXISTS slot_downtime_reason, slot_downtime_entry,
                     slot_scrap, machine_schedule,
                     machine_ideal_rates, downtime_reasons;
```

---

## What did NOT change

Worth knowing so you can rule things out if something looks off:

- `entries` and `standards` — **no schema change at all.** Not one `ALTER`.
- The `:)` / `:(` status rule, and `compute_status()`.
- `/dashboard` and `/dashboard/<zone>` — no code path touched.
- `/supervisor` — untouched.
- `/console` (single-machine) — untouched. Only `/console/<zone>` grew columns.
- Shift definitions, `SHIFT_DISPLAY_DELAY_HOURS`, `resolve_shift()`, time slots.
- CI/CD workflow, Dockerfile, compose files, `.env`.

---

## Known limits, so they're not surprises later

- **Two downtime reasons per machine per slot** on the batch form
  (`MAX_DOWNTIME_REASONS` in `app/routers/console.py`). A slot with three
  distinct causes has to fold the smallest into the largest. Raise the constant
  if that turns out to be common.
- **A downtime submission replaces that slot's whole reason set.** That's what
  makes it possible to remove a reason entered by mistake. It also means the
  form deliberately does *not* pre-fill the downtime boxes — they show what's on
  record as a grey hint instead, so only someone who actually types something
  can change it.
- **A blank production checkpoint counts as zero, not as missing.** The floor
  leaves a cell empty when the number hasn't moved, so a shift's production is
  the last reading in it and interior gaps don't change the total. One reading
  per shift is therefore enough for units and scrap — but **downtime still
  needs one entry per slot**, since it isn't cumulative and a blank there is
  genuinely unentered. Two guards apply: a shift with no readings at all stays
  unknown (that's the not-scheduled checkbox's job), and slots that haven't
  elapsed yet stay unknown rather than counting as zero.
- **Scrap must be entered for every counted slot** before Performance and
  Quality can be separated for that machine. OEE and Availability appear
  without it, because scrap cancels out of `A × P × Q`. Partial scrap gives you
  a real OEE and `—` for P and Q, which is honest rather than convenient.
- **OEE is per shift.** There's no "All Day" option like `/supervisor` has —
  the three shifts have separate planned production time, downtime and crews,
  and one number across all three would average away what the page is for.
- **The 75%-of-theoretical assumption** is applied uniformly to all 34
  machines. `machine_ideal_rates` is keyed per machine, so any of them can be
  overridden individually once the floor gives you a measured nameplate rate —
  run `09_check_ideal_rate_calibration.sql` after a few weeks of real data to
  see which ones look wrong.
- **If `/oee` reports machines "above the derived ceiling"**, that is a warning,
  not an error, and those slots still count. It means the machine beat
  `standard ÷ 0.75`, and the likelier wrong number is the ceiling — either the
  standards moved since `08` was last run, or 75% isn't the right figure for
  that machine. Run `docs/sql/10_diagnose_over_ceiling.sql`; its first query
  distinguishes the two in one look. Only production past **double** the
  ceiling is treated as a typo and excluded.
