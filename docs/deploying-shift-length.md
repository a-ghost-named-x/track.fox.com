# Deploying the shift-length change — step by step

Lets `/console/oee` set a machine's day to 8, 10 or 12-hour shifts, and makes
`/oee` judge it against that many minutes. Per the production manager,
2026-09-16. Background and the rules the floor gave are in the header of
`docs/sql/14_shift_length.sql`.

Read the ordering rule first. It is the only part that can cause an outage,
and it takes one command to avoid.

---

## ⚠ Database FIRST, push SECOND

Pushing to `main` triggers CI/CD, which builds the image and restarts the `app`
container automatically. The new code reads a table that **does not exist
yet**.

| Page | If you push before applying the SQL |
| --- | --- |
| `/oee` | **500** on its data call — it reads `machine_shift_length` on every load. |
| `/console/oee` | **500** — same table, on page load. |
| `/console` and `/console/<zone>` | **Fine.** The 2-hour rounds touch none of this. |
| `/dashboard/<zone>` | **Fine.** The floor screens are untouched. |
| `/supervisor` | Fine. |

So the boards and the rounds keep working either way. Apply the SQL first and
there is no window at all.

The migration is **additive** — one new table, and the downtime minutes cap
raised from 480 to 720. Nothing is dropped and no existing row changes, so the
currently-running image is unaffected by it and rolling the app back later
needs no database rollback.

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

> The report test now has a 12-hour machine (WS1) scoring exactly 0.75 at
> standard and a 6PM–6AM crew (WS2) stitched across two dates. If either
> fails, the geometry in `app/models.py` is wrong and `/oee` will be too.

---

## Step 2 — Apply the migration

As the **`postgres` superuser**, not `trackfox_app`, and not through CI/CD.

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/14_shift_length.sql
```

**In pgAdmin:** disable SSL mode, open the Query Tool **from the trackfox
database node** (not the server node), and run the file. It checks it is in
the right database and that file 11 has been applied before doing anything,
and it is idempotent.

**Remember pgAdmin shows only the LAST result set** when you execute a whole
file. Run the two verification queries at the bottom individually.

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
-- Expect exactly one CHECK, reading minutes > 0 AND minutes <= 720.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.shift_downtime_reason'::regclass AND contype = 'c';
```

Then confirm the app role can use the table — run **as `trackfox_app`**:

```sql
SELECT count(*) FROM machine_shift_length;
```

A `permission denied` here means the `GRANT`s in section 1 didn't apply, and
both `/oee` and `/console/oee` will 500 after deploy. Zero rows is the
expected answer — no row means 8 hours.

---

## Step 4 — Commit and push

```bash
git status && git diff --stat
```

Nothing changed in `requirements.txt`, the `Dockerfile`, compose files, or
`.env` — no new dependencies, no new environment variables.

```bash
git add -A && git commit -m "Per-machine 8/10/12h shift length for OEE"
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
   control next to its name, with 8h selected. Switch to 2nd Shift: the
   control becomes read-only text (`8h  2PM-10PM`) with a note that it is set
   on the 1st Shift page.
3. Pick a machine that ran long yesterday, set it to 12h on **yesterday's**
   1st Shift page, save. Its badge should read `12h shifts`. Untouched
   machines must not get a badge.
4. Open yesterday's **2nd Shift** page: that machine now shows `12h  6PM-6AM`
   and its Scheduled box is **unticked**. Hover it for the explanation. Leave
   it unticked unless a night crew actually ran.
5. Open **today's 3rd Shift** page (the shift that landed this morning): that
   machine's row reads *No 3rd shift — ran 12h shifts on <yesterday>*, with no
   inputs. Every other machine is normal.

**Then OEE:**

6. `/oee` for yesterday, 1st Shift — that machine has a `12h` tag beside its
   name, its Good tooltip lists six checkpoints (8AM through 6PM), and the
   Availability tooltip says `... of 720 scheduled minutes`. If it had been
   warned *"Beat the maximum derived from its standard"* before, that warning
   should now be gone — that was the whole point.
7. Switch to 2nd Shift: the machine reads `not scheduled` with the night-crew
   explanation on hover. Switch to 3rd Shift on **today's** date: it reads
   `no 3rd shift`.

---

## If something's wrong

- **`/oee` or `/console/oee` 500s after deploy** — the table is missing or the
  grant didn't apply. Re-run `14_shift_length.sql` as `postgres`; it is safe to
  re-run.
- **The 12h control saves but `/oee` still says 480 minutes** — you set it on
  the wrong date. The length is filed under the day the 1st Shift *started*;
  the 1st Shift page's date box is that day.
- **A machine's 3rd Shift row disappeared unexpectedly** — check the day
  *before* on the 1st Shift page; a 10h/12h there is what removes it. Set it
  back to 8h and the row returns (append-only, so the correction is a new
  row; nothing was deleted).
- **A 12h machine's 2nd Shift shows "not scheduled" but a crew did run** —
  that is the default; tick Scheduled on the 2nd Shift page for that date.
