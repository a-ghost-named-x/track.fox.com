# Deploying the shift-grain OEE change — step by step

Moves OEE capture from the 2-hour rounds to one end-of-shift entry, per the
floor's feedback of 2026-09-09.

Read the ordering rule first. It is the only part that can cause an outage, and
it takes one command to avoid.

---

## ⚠ Database FIRST, push SECOND

Pushing to `main` triggers CI/CD, which builds the image and restarts the `app`
container automatically. The new code reads three tables that **do not exist
yet** and eleven reason codes that **are not seeded yet**.

| Page | If you push before applying the SQL |
| --- | --- |
| `/console/oee` | **500.** It loads the reason codes on page load. |
| `/oee` | **500** on its data call. |
| `/console` and `/console/<zone>` | **Fine** — reverted to good units only, they touch none of this. |
| `/dashboard/<zone>` | **Fine.** The floor screens are untouched. |
| `/supervisor` | Fine. |

So the boards and the 2-hour rounds keep working either way. Apply the SQL first
and there is no window at all.

The migration is **additive**: it creates new tables and copies the old rows
forward. Nothing is dropped unless you deliberately uncomment section 5 of
`11_oee_shift_grain.sql`. That means the currently-running image is unaffected
by the new tables, and rolling the app back later needs no database rollback.

---

## Step 1 — Run the tests locally

No database needed; they stub it out.

```bash
python tests/test_oee_math.py
```

```bash
python tests/test_oee_report.py
```

Both end with `ALL CHECKS PASSED`. The third needs `httpx`:

```bash
python -m pip install -r requirements-dev.txt
```

```bash
python tests/test_routes_smoke.py
```

Run that one **from the repo root** — Jinja resolves `app/templates`
relatively. It ends with `ALL SMOKE CHECKS PASSED`.

> If `test_oee_math.py` fails on `rollup A x P x Q == oee`, stop. That assertion
> is what catches broken aggregation, and a failure means the numbers on `/oee`
> are wrong even though the page renders happily.

---

## Step 2 — Apply the two migrations

As the **`postgres` superuser**, not `trackfox_app`, and not through CI/CD.

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/11_oee_shift_grain.sql
```

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/12_seed_shift_downtime_reasons.sql
```

`ON_ERROR_STOP=1` matters — without it psql plows through a failed statement and
leaves a half-created schema with no obvious error.

**In pgAdmin:** disable SSL mode, and run the files in this order. `12` checks
for its table up front and stops with an instruction if `11` hasn't run, so a
wrong order costs nothing but a re-run. Both are idempotent.

**Remember pgAdmin shows only the LAST result set** when you execute a whole
file, so the verification queries at the bottom of each file will be the only
thing you see. Run them individually to read the others.

---

## Step 3 — Verify the migration

```sql
-- Old vs new. New counts should be LOWER (four slots collapse into one shift)
-- and must not be zero if the old tables had anything in them.
SELECT 'slot_scrap' AS t, count(*) FROM slot_scrap
UNION ALL SELECT 'shift_scrap',           count(*) FROM shift_scrap
UNION ALL SELECT 'slot_downtime_entry',   count(*) FROM slot_downtime_entry
UNION ALL SELECT 'shift_downtime_entry',  count(*) FROM shift_downtime_entry
UNION ALL SELECT 'slot_downtime_reason',  count(*) FROM slot_downtime_reason
UNION ALL SELECT 'shift_downtime_reason', count(*) FROM shift_downtime_reason
ORDER BY t;
```

```sql
-- Expect 11 active codes, all is_planned = false, in the floor's sheet order.
SELECT sort_order, code, label, is_planned
FROM downtime_reasons WHERE active ORDER BY sort_order;
```

```sql
-- Expect 11 active, 10 retired. The old codes are kept, not deleted — the rows
-- migrated from slot grain still reference them.
SELECT active, count(*) FROM downtime_reasons GROUP BY active ORDER BY active DESC;
```

Then confirm the app role can read the new tables — run **as `trackfox_app`**:

```sql
SELECT count(*) FROM shift_scrap;
SELECT count(*) FROM shift_downtime_entry;
SELECT count(*) FROM shift_downtime_reason;
```

A `permission denied` here means the `GRANT`s at the bottom of section 4 didn't
apply, and `/console/oee` will 500 after deploy.

---

## Step 4 — Commit and push

```bash
git checkout -b feature/shift-oee
```

```bash
git status && git diff --stat
```

Expected — `app/routers/console.py` and `app/templates/console_batch.html`
should show as **reverted** to their pre-OEE state, not further modified.

```bash
git add -A && git commit -m "Move OEE capture to end-of-shift; revert /console to production only"
```

```bash
git checkout main && git merge --no-ff feature/shift-oee && git push origin main
```

Nothing changed in `requirements.txt`, the `Dockerfile`, compose files, or
`.env` — no new dependencies, no new environment variables.

---

## Step 5 — Watch the deploy

```bash
gh run watch
```

```bash
sudo docker compose -f /opt/track-fox-com/docker-compose.yml ps
```

```bash
sudo docker compose -f /opt/track-fox-com/docker-compose.yml logs --tail=50 app
```

---

## Step 6 — Verify, in this order

**The boards first** — they're what the floor is looking at:

1. `/dashboard/b3` — grid renders, numbers present, still polling. Nothing about
   this page changed; you're confirming you didn't break it.

**Then the 2-hour rounds**, which are the thing that must feel unchanged:

2. `/console/b3` — should show **Machine, Operator, Units produced, Issue** and
   nothing else. No Scrap column, no Downtime column, no Scheduled checkbox. If
   any of those are still there, the revert didn't take.
3. Enter units for one machine as normal and confirm it appears on
   `/dashboard/b3`.

**Then the new form:**

4. `/console/oee` — all 34 machines grouped by zone. If this 404s, the router
   registration order in `app/main.py` is wrong: `console_oee` must be included
   **before** `console`, or `/console/oee` gets matched as a zone named "oee".
5. Open one machine's Downtime, tick two or three reasons, put minutes on each.
   The collapsed summary should read something like `3 reasons · 80 min`.
6. Save. Only the machines you changed should show a "saved" badge.

**Then OEE:**

7. `/oee` — one row per machine, no per-slot columns. The machine you just
   entered should now have a real OEE.

**And the menu:**

8. `/dashboard` — three sections listing every URL in the app.

---

## Step 7 — Retire the old tables (later, not now)

Once `/oee` has been checked against a known day and you're happy, uncomment
and run section 5 of `11_oee_shift_grain.sql`. Nothing in the app reads those
tables after this deploy, but they are the only copy of the pre-migration data,
so there is no hurry.

---

## Rollback

**App code** — revert and push; CI/CD redeploys the previous image:

```bash
git revert --no-commit HEAD && git commit -m "Revert shift-grain OEE" && git push origin main
```

Note the previous image expects the OLD slot-grain tables, so **don't drop them
until you're confident** (Step 7). While both sets exist, either image runs.

**Reason codes** — the old build reads `downtime_reasons` and would show the 11
new codes on its 2-hour form. Harmless, but to restore the old dropdown:

```sql
UPDATE downtime_reasons SET active = true
WHERE code IN ('MATL','MECH','CHGOVR','TOOL','ELEC','QUAL','OPER','OTHER','PM','NOSCHED');
UPDATE downtime_reasons SET active = false
WHERE code IN ('ROLL_CHANGE','SETUP','FAAR','EQUIP_FAIL','OPER_ADJUST','DEFECT_MAT',
               'LACK_MAT','LACK_OPER','DELIVERY','REGISTRATION','SHIFT_START');
```

---

## What changed, in one place

| | Before | After |
| --- | --- | --- |
| Downtime + scrap entered | every 2 hours, on `/console/<zone>` | once per shift, on `/console/oee` |
| Scrap | cumulative per slot | one total per shift |
| Reason picker | 2 dropdowns | 11 checkboxes, minutes on each |
| Scheduled checkbox | `/console/<zone>` | `/console/oee` |
| `/oee` slot columns | per-slot OEE | gone; one row per machine per shift |
| Reason codes | 10 placeholders | the floor's 11, all unplanned |
| `/dashboard` | 5 zone links | full menu of every URL |

---

## Known limits

- **All 11 codes are unplanned, and that is correct rather than a compromise.**
  Planned maintenance here is given a whole shift, never a slice of one, so it
  is never a downtime *reason* — it is a machine that wasn't scheduled to run.
  Untick **Scheduled** on `/console/oee` and the machine drops out of that
  shift's rollup entirely: not 0%, absent. That is the planned-downtime
  mechanism, and it is why Planned Production Time being the full shift costs
  nothing. (`is_planned` is still honoured end to end, and `was_planned` is
  snapshotted per row, so adding a planned code later would work — there is
  just no need for one.)
- **No catch-all reason.** Someone with a cause outside the 11 has nowhere to
  put it. Watch for people forcing things into the nearest wrong code; if that
  starts happening, an `OTHER` with a required note is a one-row seed.
- **`FAAR` is seeded with the acronym as its own label**, because nobody has
  told me what it expands to. Change `label` in the seed file whenever.
- **A blank production checkpoint still means "unchanged"** on the 2-hour form.
  That rule did not move — it applies to production units only, and scrap no
  longer carries forward because there's one reading per shift now.
