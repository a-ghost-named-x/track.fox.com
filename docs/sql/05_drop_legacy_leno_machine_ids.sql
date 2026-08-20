-- track.fox.com v2 — Retire the legacy A1-A7 Leno machine IDs
--
-- The Leno machines are AS1-AS7 (the floor's own naming, per the standards
-- spreadsheet). An earlier revision of app/models.py listed them as A1-A7,
-- and the placeholder version of 04_seed_new_machines_standards.sql seeded
-- their standards under those A1-A7 IDs with standard_units = 0.
--
-- Now that MACHINE_IDS / DASHBOARD_ZONES use AS1-AS7 and 04 seeds real
-- numbers under AS1-AS7, the old A1-A7 rows are orphans: nothing in the app
-- ever looks them up again.
--
-- Run 04_seed_new_machines_standards.sql FIRST, then this. Safe to run even
-- if the placeholder was never applied — it simply deletes nothing.

-- ---------------------------------------------------------------------------
-- STEP 1 — Look before you delete. Run this on its own first.
-- ---------------------------------------------------------------------------
SELECT 'standards' AS table_name, machine_id, count(*) AS row_count
FROM standards
WHERE machine_id ~ '^A[1-7]$'
GROUP BY machine_id
UNION ALL
SELECT 'entries', machine_id, count(*)
FROM entries
WHERE machine_id ~ '^A[1-7]$'
GROUP BY machine_id
ORDER BY table_name, machine_id;

-- ---------------------------------------------------------------------------
-- STEP 2 — Drop the orphaned standards rows.
--
-- `standards` is reference data, not history, so deleting is the right move
-- here — unlike `entries`, which is append-only by design. The regex is
-- anchored so it can only ever match A1..A7 and never AS1..AS7.
-- ---------------------------------------------------------------------------
DELETE FROM standards
WHERE machine_id ~ '^A[1-7]$';

-- ---------------------------------------------------------------------------
-- STEP 3 (OPTIONAL, and only if STEP 1 showed entries rows) — deliberately
-- left commented out. Read this before uncommenting anything.
--
-- If anyone logged production against A1-A7, those rows are still in
-- `entries`. They will simply stop rendering: the dashboard skips any
-- machine_id with no matching row in the grid (see dashboard.js, "machine
-- not in the configured MACHINE_IDS list yet"). Nothing breaks; the history
-- just goes quiet.
--
-- Renaming them to AS1-AS7 would bring that history back onto the Leno
-- dashboard, but it is an in-place UPDATE of a table the architecture
-- treats as append-only, so it is your call, not the script's:
--
--   UPDATE entries
--   SET machine_id = 'AS' || substring(machine_id from 2)
--   WHERE machine_id ~ '^A[1-7]$';
--
-- Note what it does NOT fix: `status` is computed at write-time and stored
-- on the row, so any entry saved while the standard was 0 is permanently
-- ':)' regardless of how many units it recorded. Renaming carries that
-- bogus green forward. Recomputing it against the real standards would be:
--
--   UPDATE entries e
--   SET status = CASE WHEN e.units_produced >= s.standard_units
--                     THEN ':)' ELSE ':(' END
--   FROM standards s
--   WHERE s.machine_id = e.machine_id
--     AND s.time_slot = e.time_slot
--     AND e.machine_id ~ '^(AS[1-7]|P[1-4])$';
--
-- That rewrites history rather than appending to it. Recommended only if
-- the floor wants past Poly/Leno days to read accurately; leaving it alone
-- and letting correct data start from today is equally defensible.
