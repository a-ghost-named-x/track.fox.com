# Deploying short days and the 7/30-day Pareto — step by step

Two changes from the production manager, 2026-09-23:

1. **Short days.** `/console/oee` gets a box after `8h | 10h | 12h` where a
   whole number of hours, 1 to 6, can be typed. It puts that machine's day on
   6-hour shifts (1st 6AM–12PM, 2nd 12PM–6PM, 3rd 6PM–12AM) and OEE judges it
   against the hours typed. Rules and reasoning: "SHORT DAYS" in
   `app/models.py`.
2. **Week and month Pareto.** The downtime chart at the bottom of `/oee` gets
   a Period choice: *This shift*, *7 days*, *30 days* — rolling, ending on
   the date in the date box, all shifts added together.

One SQL file: `docs/sql/16_short_shifts.sql`. It only widens a CHECK so the
database accepts 1–6 as a shift length. No new table, no grants, no data
moved.

---

## ⚠ Database FIRST, push SECOND

| Page | If you push before applying the SQL |
| --- | --- |
| `/console/oee` | Loads fine. **The first save that includes a short day 500s** part-way through — machines above it saved, the rest not. 8/10/12h saves are unaffected. |
| `/oee` | **Fine**, including the new Pareto periods. |
| everything else | **Fine.** The rounds and the floor screens are untouched. |

So the risk is small and only on one action, but applying the file first
leaves no window at all.

---

## Step 1 — Run the tests locally

No database needed; they stub it out.

```bash
python tests/test_oee_math.py
```

```bash
python tests/test_oee_report.py
```

```bash
python tests/test_routes_smoke.py
```

Run them **from the repo root**. Each ends with `ALL CHECKS PASSED` /
`ALL SMOKE CHECKS PASSED`.

> The report test now has a short Saturday: FM1 at 6 hours scoring exactly
> 0.75 at standard, FM2 at 4 hours judged on 240 minutes, FM3 with all three
> 6-hour crews (the 12PM crew counting from zero, a 5-hour 3rd Shift ending
> at midnight), and the 7/30-day Pareto over a Tuesday and that Saturday.

---

## Step 2 — Apply the migration

As the **`postgres` superuser**, not `trackfox_app`, and not through CI/CD.

```bash
psql -h lost-woods -U postgres -d trackfox -v ON_ERROR_STOP=1 -f docs/sql/16_short_shifts.sql
```

**In pgAdmin:** disable SSL mode, open the Query Tool **from the trackfox
database node** (not the server node), and run the file. It checks it's in the
right database and that 14 and 15 are applied, and it's safe to re-run.

---

## Step 3 — Verify the migration

Run this on its own (pgAdmin only shows the last result of a whole file):

```sql
-- Expect two rows: the shift-name CHECK from 15, and one reading
-- shift_hours >= 1 AND shift_hours <= 6 OR shift_hours = ANY (ARRAY[8, 10, 12]).
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.machine_shift_length'::regclass AND contype = 'c'
ORDER BY conname;
```

Nothing else needs checking — no grants changed.

---

## Step 4 — Commit and push

```bash
git status && git diff --stat
```

No changes to `requirements.txt`, the `Dockerfile`, compose files or `.env`.

```bash
git add -A && git commit -m "Short days (1-6h on 6-hour shifts) on /console/oee; 7/30-day Pareto on /oee"
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

**The boards first** — nothing about them changed:

1. `/dashboard/b3` — grid renders, still polling.

**Then the form** — use the most recent **Saturday**, or any past date you
don't mind correcting afterwards:

2. `/console/oee` on **1st Shift** — every machine's length control now reads
   `8h | 10h | 12h | [  ]h`. Under the Shift length hint there's a second
   *Short day* note ending in **Set every machine to [ ] hours  Apply**.
3. Type `6` in that box and press **Apply**: every row's short box fills with
   6 and shows `6AM-12PM` under it; the note says *Set 34 machines to 6h — not
   saved until you press Save.* Change one machine to `4` by typing in its own
   box — it should read `6AM-10AM`, and its downtime minutes can't exceed 240.
4. Enter your employee number and **Save**. Badges read `6h short day` /
   `4h short day`.
5. Open the same date's **2nd Shift** page: those machines show the short day
   picked with `6`, `12PM-6PM`, *follows 1st*, and **Scheduled unticked**
   (hover it: *On a short day this shift … starts unticked*). Leave them
   unticked unless an afternoon crew ran.
6. Open the same date's **3rd Shift** page: those machines now have a length
   control (`8h | [ 6 ]h`, `6PM-12AM`), unticked. Machines on an ordinary
   day still show a read-only `8h 10PM-6AM`.
7. Sanity check the overlap rule: on the **2nd Shift** page of an ordinary
   weekday, the short-day option is struck through on every row (hover: *would
   start inside the 8h shift before it*).

**Then `/oee`:**

8. The Saturday, 1st Shift: the short machines carry a `6h` / `4h` label;
   hover it for the span and the minutes. The Availability tooltip reads
   *Ran … of 360 scheduled minutes* (240 for the 4-hour one).
9. Same date, 2nd Shift: those machines read `not scheduled`, with the
   short-day explanation on hover.
10. Scroll to **Downtime by reason**. Click **7 days**: the heading reads
    e.g. *all machines · Wed, 9/17 – Tue, 9/23, all shifts*, the total shows
    minutes and hours, and a line under the bars says how many machine-shifts
    it's built from — amber if any shifts reported production with no
    downtime entered. The URL gains `&period=7`.
11. Click a zone chip, then **30 days**, then **This shift** — the bars re-sum
    each time; *This shift* hides the line under the bars again.

---

## If something's wrong

- **`/console/oee` 500s when saving a short day** — 16 hasn't been applied, or
  was applied to the wrong database. Run it as `postgres` from the trackfox
  database node.
- **The short-day option is struck through on a 2nd or 3rd Shift row** — the
  shift before it is 8 hours or more on that machine's day, and a 6-hour
  window would start inside it. Make the earlier shift a short day first.
- **"4 is typed in the short-day box but 8h is picked"** — the box only
  counts when the short day is the choice. Pick it, or clear the box. (With
  JavaScript on, typing in the box picks it for you; this is the no-JS path.)
- **A short machine's OEE looks too good** — check the hours typed are the
  hours it was *scheduled*, not the hours it ran. A no-show inside scheduled
  time belongs in Lack of Operator.
- **The week/month line says many shifts are missing downtime** — those are
  real shifts with production and no end-of-shift entry. Enter them on
  `/console/oee` for their date and shift; the chart picks them up on reload.
