-- track.fox.com v2 — Shift length per SHIFT, and the review side moves to the
-- production day
--
-- Follows 14_shift_length.sql, which must already be applied.
--
-- WHY
-- ---
-- Two refinements from the production manager (2026-09-18):
--
--   1. A machine's shifts can differ in length within one day. C1 can run an
--      8-hour 1st Shift, sit idle 2PM-6PM, and have a 12-hour crew come in at
--      6PM. File 14 stored one length per (machine, day), which locked the
--      whole day to one pattern. The length now belongs to each shift.
--
--      The rule that makes this safe: a shift's length decides where it
--      starts (the second 12-hour shift of the day is 6PM-6AM, the second
--      10-hour one 4PM-2AM), and a later shift can be AS LONG OR LONGER than
--      the one before it, never shorter — or it would start inside it. The
--      app enforces that on every save (shift_length_conflict in
--      app/models.py); the database only stores the values.
--
--   2. "3rd Shift, Thursday" means Thursday 10PM through Friday 6AM. The
--      review pages (/oee, /supervisor) and the end-of-shift form
--      (/console/oee) used to file the overnight shift under the morning it
--      LANDS on, because that is how the 2-hour rounds date its 12AM-6AM
--      checkpoints. The rounds and the floor screens keep that dating — it
--      is baked into `entries` and the boards' "today" filter — but the
--      review side now uses the day the shift STARTED throughout.
--
--      That means every 3rd Shift row in the OEE tables entered so far is
--      filed one day late by the new reading, and section 3 moves them back.
--
-- WHAT THIS DOES
-- --------------
--   1. machine_shift_length gains a `shift` column. Rows written by file 14's
--      app (if any) meant "the day", which under the new inheritance rule is
--      exactly "the 1st Shift", so they become 1st Shift rows.
--   2. A tiny `applied_migrations` table, because section 3 is a data move
--      that must run exactly once — re-running it would shift the same rows
--      a second day. Every other file here is idempotent by construction;
--      this is the first that needs a memory.
--   3. Re-keys 3rd Shift rows in shift_scrap, shift_downtime_entry and
--      machine_schedule to entry_date - 1 day. Once.
--
-- HOW TO APPLY
-- ------------
--   psql/pgAdmin as the `postgres` superuser, NOT trackfox_app, and not
--   through CI/CD. Apply BEFORE pushing the app change: the new code reads
--   the `shift` column on every /oee and /console/oee load.
--
--   Safe to re-run: the column add and constraint swap are guarded, and the
--   data move checks applied_migrations first.


-- ===========================================================================
-- GUARD: RIGHT DATABASE, AND 14 APPLIED
-- ===========================================================================
DO $$
BEGIN
    IF to_regclass('public.entries') IS NULL
       OR to_regclass('public.standards') IS NULL THEN
        RAISE EXCEPTION
            'This is not the track.fox.com database. Connected to "%" as "%". In pgAdmin, open the Query Tool from the trackfox database node, not the server node.',
            current_database(), current_user;
    END IF;
    IF to_regclass('public.machine_shift_length') IS NULL THEN
        RAISE EXCEPTION
            'machine_shift_length does not exist. This IS the right database, so apply docs/sql/14_shift_length.sql first, then re-run this file.';
    END IF;
END
$$;


-- ===========================================================================
-- 1. ONE LENGTH PER SHIFT
--
-- Existing rows meant "the whole day". Under inheritance (each shift
-- defaults to the one before it) that is the same as setting the 1st Shift,
-- so they are backfilled as 1st Shift rows and nothing changes for them.
-- ===========================================================================
ALTER TABLE machine_shift_length ADD COLUMN IF NOT EXISTS shift TEXT;

UPDATE machine_shift_length SET shift = '1st Shift' WHERE shift IS NULL;

ALTER TABLE machine_shift_length ALTER COLUMN shift SET NOT NULL;

-- Same CHECK the other OEE tables carry, added only if it isn't there yet.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.machine_shift_length'::regclass
          AND conname = 'machine_shift_length_shift_check'
    ) THEN
        ALTER TABLE machine_shift_length
            ADD CONSTRAINT machine_shift_length_shift_check
            CHECK (shift IN ('1st Shift', '2nd Shift', '3rd Shift'));
    END IF;
END
$$;

-- "Newest row for this machine, day and shift" — the lookup every reader
-- does. The old (machine, day) index is left in place; it still serves the
-- by-date scan and costs nothing.
CREATE INDEX IF NOT EXISTS idx_machine_shift_length_shift_lookup
    ON machine_shift_length (machine_id, entry_date, shift, created_at DESC);


-- ===========================================================================
-- 2. A MEMORY FOR ONE-SHOT DATA MOVES
--
-- Superuser only — the app never touches it. If a future file needs to move
-- data exactly once, it records itself here the same way.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS applied_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ===========================================================================
-- 3. RE-KEY THE 3RD SHIFT ROWS TO THE DAY THE SHIFT STARTED
--
-- Everything the end-of-shift form saved for a 3rd Shift was filed under the
-- morning it landed on. From now on the form, /oee and /supervisor read
-- "3rd Shift, D" as D 10PM -> D+1 6AM, so those rows move back one day.
--
-- `entries` is deliberately NOT touched. The rounds still date the 12AM-6AM
-- checkpoints by the morning they land on; the review side reads two dates
-- and stitches the shift together (shift_slot_dates in app/models.py).
--
-- Runs once. The marker is written in the same transaction as the moves, so
-- a failure part-way leaves neither.
-- ===========================================================================
DO $$
DECLARE
    moved_scrap     integer;
    moved_downtime  integer;
    moved_schedule  integer;
BEGIN
    IF EXISTS (SELECT 1 FROM applied_migrations WHERE name = '15_third_shift_to_production_day') THEN
        RAISE NOTICE '3rd Shift rows already re-keyed; skipping.';
        RETURN;
    END IF;

    UPDATE shift_scrap
       SET entry_date = entry_date - INTERVAL '1 day'
     WHERE shift = '3rd Shift';
    GET DIAGNOSTICS moved_scrap = ROW_COUNT;

    UPDATE shift_downtime_entry
       SET entry_date = entry_date - INTERVAL '1 day'
     WHERE shift = '3rd Shift';
    GET DIAGNOSTICS moved_downtime = ROW_COUNT;

    UPDATE machine_schedule
       SET entry_date = entry_date - INTERVAL '1 day'
     WHERE shift = '3rd Shift';
    GET DIAGNOSTICS moved_schedule = ROW_COUNT;

    -- machine_shift_length never held a 3rd Shift row before this file (it
    -- had no shift column), so there is nothing of its own to move.

    INSERT INTO applied_migrations (name) VALUES ('15_third_shift_to_production_day');

    RAISE NOTICE 'Re-keyed 3rd Shift rows: % scrap, % downtime, % schedule.',
        moved_scrap, moved_downtime, moved_schedule;
END
$$;


-- ===========================================================================
-- VERIFICATION — run these individually.
-- pgAdmin only shows the LAST result set when a whole file is executed.
-- ===========================================================================

-- The column and its CHECK. Expect one row with the three shift names.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.machine_shift_length'::regclass AND contype = 'c'
ORDER BY conname;

-- The move happened exactly once. Expect one row.
SELECT * FROM applied_migrations WHERE name = '15_third_shift_to_production_day';

-- Sanity: 3rd Shift downtime should now sit one day EARLIER than the 1st
-- Shift entered the same morning. Pick a recent day you remember and eyeball.
SELECT entry_date, shift, count(*) AS submissions
FROM shift_downtime_entry
GROUP BY entry_date, shift
ORDER BY entry_date DESC, shift
LIMIT 12;
