# Deploying the shift-length change — step by step

Lets `/console/oee` set each of a machine's shifts to 8, 10 or 12 hours, and
makes `/oee` judge it against that many minutes. Per the production manager,
2026-09-16, refined 2026-09-18. Two SQL files: `14_shift_length.sql` (the
table) and `15_shift_length_per_shift.sql` (per-shift lengths, and the review
side moving to "the day the shift started"). Background and the floor's rules
are in their headers and in `app/models.py` under "Shift LENGTH".

**If you already applied 14** (you did, 2026-09-18): run 15 and skip nothing
else — the app change needs both.

Read the ordering rule first. It is the only part that can cause an outage,
and it takes one command to avoid.

---

## ⚠ Database FIRST, push SECOND

Pushing to `main` triggers CI/CD, which builds the image and restarts the `app`
container automatically. The new code reads a table and a column that **do
not exist yet**.

| Page | If you push before applying the SQL |
| --- | --- |
| `/oee` | **500** on its data call — it reads `machine_shift_length.shift` on every load. |
| `/console/oee` | **500** — same column, on page load. |
| `/supervisor` | **Fine**, but its 3rd Shift shows the wrong night until the app is deployed (see below). |
| `/console` and `/console/<zone>` | **Fine.** The 2-hour rounds touch none of this. |
| `/dashboard/<zone>` | **Fine.** The floor screens are untouched. |

So the boards and the rounds keep working either way. Apply the SQL first and
there is no window at all.

File 14 is **additive** (one new table, the downtime cap raised from 480 to
720). File 15 adds a column and then does one **data move**: every 3rd Shift
row in `shift_scrap`, `shift_downtime_entry` and `machine_schedule` is
re-keyed to the day before, because "3rd Shift, Thursday" now means Thursday
night rather than the shift that ended Thursday morning. It runs once (a
marker in `applied_migrations` stops a second run) and touches nothing in
`entries`. Between running 15 and the app restarting, the OLD app reads those
moved rows one day off on `/oee` — a window of a few minutes if you push
straight after, and only on that review page.

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

> The report test has a 12-hour machine (WS1) scoring exactly 0.75 at
> standard, a 6PM–6AM crew (WS2) stitched across two dates, the manager's
> 8h-then-12h case (WS3), and C1's ordinary 3rd Shift read from the next
> morning's entries. If any fails, the geometry in `app/models.py` is wrong
> and `/oee` will be too.

---

## Step 2 — Apply the migrations

As the **`postgres` superuser**, not `trackfox_app`, and not through CI/CD.
Skip 14 if it is already applied (15 checks, and tells you if it isn't).

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/14_shift_length.sql
```

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/15_shift_length_per_shift.sql
```

**In pgAdmin:** disable SSL mode, open the Query Tool **from the trackfox
database node** (not the server node), and run each file. Both check they are
in the right database and that the file before them has been applied, and
both are safe to re-run — 15's data move records itself in
`applied_migrations` and skips on a second run.

**Remember pgAdmin shows only the LAST result set** when you execute a whole
file. Run the verification queries at the bottom individually. 15 also
prints a NOTICE with how many rows it moved — in pgAdmin that is on the
Messages tab.

---

## Step 3 — Verify the migration

```sql
-- Expect two rows: INSERT and SELECT.
SELECT privilege_type
FROM information_schema.role_table_grants
WHERE table_name = 'machine_shift_length' AND grantee = 'trackfox_app'
ORDER BY privilege_type;
```

```sql
-- Expect a `shift` column, NOT NULL, with a CHECK naming the three shifts.
SELECT column_name, is_nullable
FROM information_schema.columns
WHERE table_name = 'machine_shift_length' ORDER BY ordinal_position;
```

```sql
-- Expect exactly one row: the move ran once.
SELECT * FROM applied_migrations;
```

```sql
-- Expect exactly one CHECK, reading minutes > 0 AND minutes <= 720.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.shift_downtime_reason'::regclass AND contype = 'c';
```

Then confirm the app role can use the table — run **as `trackfox_app`**:

```sql
SELECT count(*) FROM machine_shift_length;
```

A `permission denied` here means the `GRANT`s in 14 didn't apply, and both
`/oee` and `/console/oee` will 500 after deploy. Zero rows is fine — no row
means "inherit", which for the 1st Shift means 8 hours.

---

## Step 4 — Commit and push

```bash
git status && git diff --stat
```

Nothing changed in `requirements.txt`, the `Dockerfile`, compose files, or
`.env` — no new dependencies, no new environment variables.

```bash
git add -A && git commit -m "Per-shift 8/10/12h shift length for OEE; review pages use the production day"
```

```bash
git push origin main
```

---

## Step 5 — Watch the deploy

```bash
gh run watch
```

```bash
sudo docker compose -f /opt/track-fox-com/docker-compose.yml logs --tail=50 app
```

---

## Step 6 — Verify, in this order

**The boards first** — nothing about them changed; confirm you didn't break
them:

1. `/dashboard/b3` — grid renders, still polling.

**Then the form:**

2. `/console/oee` on **1st Shift** — every machine has an `8h | 10h | 12h`
   control next to its name, with 8h selected. Switch to 2nd Shift: same
   control, each row marked *follows 1st*. Switch to 3rd Shift: read-only
   `8h  10PM-6AM`, and the line under the toggle reads *3rd Shift of <date>:
   10PM <date> to 6AM <next day>*.
3. Pick a machine that ran long yesterday, set it to 12h on **yesterday's**
   1st Shift page, save. Its badge should read `12h shifts`. Untouched
   machines must not get a badge.
4. Open yesterday's **2nd Shift** page: that machine shows 12h selected with
   8h and 10h struck through (hover: *would start inside it*), and its
   Scheduled box is **unticked**. Leave it unticked unless a night crew ran.
5. Open **yesterday's 3rd Shift** page (same date — the night that started
   yesterday): that machine's row reads *No 3rd shift — 2nd Shift ran 12h
   (6PM-6AM)*, with no inputs. Every other machine is normal.
6. The manager's case: on some other machine's **2nd Shift** page pick 12h.
   Scheduled ticks itself. Save: badge `12h shifts`, `scheduled`. Its 1st
   Shift is untouched (still 8h).
7. Open the **3rd Shift** page with no date in the URL before noon: the date
   box should read **yesterday**.

**Then OEE:**

8. `/oee` for yesterday, 1st Shift — every machine has an `8h` / `10h` /
   `12h` label beside its name; the 12h one's Good tooltip lists six
   checkpoints (8AM through 6PM), and its Availability tooltip says `... of
   720 scheduled minutes`. If it had been warned *"Beat the maximum derived
   from its standard"* before, that warning should now be gone — that was
   the whole point.
9. Switch to 2nd Shift: the 12h machine reads `not scheduled` with the
   night-crew explanation on hover; the manager's-case machine has a real
   OEE stitched across midnight (Good tooltip shows `12AM (<next day>)`).
   Switch to 3rd Shift on the **same** date: both read `no 3rd shift`; every
   8h machine's 3rd Shift is last night's, read from this morning's entries.
10. The Pareto at the bottom: click a zone chip, then *Pick machines* and
    untick a few — the bars re-sum and the heading names the scope.

**Then `/supervisor`:**

11. Yesterday, 3rd Shift — the grid shows last night (10PM yesterday → 6AM
    today), and the note under the toggle says so. All Day is 6AM → 6AM.

---

## If something's wrong

- **`/oee` or `/console/oee` 500s after deploy** — the table is missing or the
  grant didn't apply. Re-run `14_shift_length.sql` as `postgres`; it is safe to
  re-run.
- **The 12h control saves but `/oee` still says 480 minutes** — you set it on
  the wrong date or the wrong shift. The length is filed under the day the
  shift *started*, per shift.
- **A machine's 3rd Shift row disappeared** — its 2nd Shift on that date is
  10h or 12h (set, or inherited from a 10h/12h 1st Shift). Set the 2nd Shift
  back to 8h and the row returns (append-only, so the correction is a new
  row; nothing was deleted).
- **Can't pick 8h on the 2nd Shift page** — the 1st Shift is longer than
  that; a shorter 2nd Shift would start inside it. Shorten the 1st first.
- **A 12h machine's 2nd Shift shows "not scheduled" but a crew did run** —
  that is the default; tick Scheduled on the 2nd Shift page for that date.
- **`/oee`'s 3rd Shift numbers look like the wrong night** — 15 hasn't been
  run, or the app wasn't redeployed after it. The two must go together.
