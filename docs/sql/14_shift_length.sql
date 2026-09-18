-- track.fox.com v2 — Per-machine, per-day shift length (8, 10 or 12 hours) for OEE
--
-- SUPERSEDED IN PART BY 15_shift_length_per_shift.sql (2026-09-18): the length
-- is now per SHIFT, not per day, and the review side files the 3rd Shift under
-- the day it STARTED. This file still creates the table and raises the
-- downtime cap, so it must run first; read 15's header for the current model.
--
-- WHY
-- ---
-- Per the production manager (2026-09-16): with the current staffing, a machine
-- is often run for 10 or 12 hours by one crew instead of three 8-hour shifts.
-- The work day always starts at 6AM and a day's pattern never mixes — C1 runs
-- 6AM-6PM and then either a second 12-hour crew comes in at 6PM (uncommon) or
-- the machine is off until 6AM. Nobody runs an 8-hour shift after a 12-hour
-- one.
--
-- The 2-hour rounds and the floor screens stay on the 8-hour rotation
-- regardless; the crew simply keeps writing the running count into the 4PM
-- and 6PM boxes. OEE, though, was judging those machines against 480 minutes
-- while they ran 720, which is exactly the "beat the maximum derived from its
-- standard" warning WS1, WS2 and P1 raised on 2026-09-15.
--
-- WHAT THIS ADDS
-- --------------
--   machine_shift_length    one row per (machine, day) that is NOT the 8-hour
--                           default. Absence means 8. Append-only, newest wins,
--                           like machine_schedule.
--   shift_downtime_reason   the minutes cap goes from 480 to 720, since a
--                           12-hour shift can genuinely lose more than 480.
--                           The per-shift cap is still enforced by the app
--                           against that machine's actual length.
--
-- The date on a row is the PRODUCTION DAY: the calendar date the 1st Shift
-- started on (6AM). It governs that date's 1st and 2nd Shifts — and, because
-- the app files the overnight shift under the morning it LANDS on, it is the
-- NEXT date's 3rd Shift row that a 10/12-hour day removes. See
-- production_day_for() in app/models.py.
--
-- HOW TO APPLY
-- ------------
--   psql/pgAdmin as the `postgres` superuser, NOT trackfox_app, and not
--   through CI/CD. Apply BEFORE pushing the app change: /oee and /console/oee
--   both read the new table on load and would 500 without it.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, and the constraint swap in section 2
-- drops whatever cap is there before adding the new one.


-- ===========================================================================
-- GUARD: ARE WE EVEN IN THE RIGHT DATABASE, AND HAS 11 BEEN APPLIED?
--
-- Same two checks as 11_oee_shift_grain.sql, for the same reason: a Query Tool
-- opened from pgAdmin's SERVER node lands in the `postgres` maintenance
-- database, where the only symptom is a bare 'relation does not exist'.
-- ===========================================================================
DO $$
BEGIN
    IF to_regclass('public.entries') IS NULL
       OR to_regclass('public.standards') IS NULL THEN
        RAISE EXCEPTION
            'This is not the track.fox.com database. Connected to "%" as "%", which has no `entries`/`standards` tables. In pgAdmin, open the Query Tool from the trackfox database node, not the server node.',
            current_database(), current_user;
    END IF;
    IF to_regclass('public.shift_downtime_reason') IS NULL THEN
        RAISE EXCEPTION
            'shift_downtime_reason does not exist. This IS the right database, so apply docs/sql/11_oee_shift_grain.sql first, then re-run this file.';
    END IF;
END
$$;


-- ===========================================================================
-- 1. SHIFT LENGTH, per machine per production day
--
-- Only exceptions are stored: no row means the machine ran the normal three
-- 8-hour shifts. A row of 8 is still accepted, because that is how someone
-- undoes a 12 entered by mistake (append-only, so the correction is a new row
-- rather than an UPDATE).
--
-- The CHECK is the whole menu. Adding a length here also needs
-- SHIFT_LENGTH_HOURS in app/models.py, and the slot geometry only works for
-- even hours (every slot is a 2-hour window).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS machine_shift_length (
    id           BIGSERIAL PRIMARY KEY,
    machine_id   TEXT NOT NULL,
    entry_date   DATE NOT NULL,
    shift_hours  INTEGER NOT NULL CHECK (shift_hours IN (8, 10, 12)),
    entered_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Newest row for this machine on this day", the same DISTINCT ON pattern
-- every other OEE table is read with.
CREATE INDEX IF NOT EXISTS idx_machine_shift_length_lookup
    ON machine_shift_length (machine_id, entry_date, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_machine_shift_length_date
    ON machine_shift_length (entry_date);

GRANT SELECT, INSERT ON machine_shift_length TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE machine_shift_length_id_seq TO trackfox_app;


-- ===========================================================================
-- 2. RAISE THE DOWNTIME MINUTES CAP FROM 480 TO 720
--
-- The column's CHECK was written as `minutes <= 480` when every shift was
-- 8 hours. A 12-hour shift can legitimately lose up to 720. The app still
-- rejects a submission whose minutes exceed THAT machine's shift length on
-- THAT day (create_shift_downtime in app/db/oee.py); this is only the
-- database's outer bound.
--
-- The constraint is found by content rather than by name, because the
-- inline CHECK in file 11 got an auto-generated name that could in principle
-- differ between installs. Dropping and re-adding is what makes re-running
-- this file harmless.
-- ===========================================================================
DO $$
DECLARE
    con record;
BEGIN
    FOR con IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'public.shift_downtime_reason'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%minutes%'
    LOOP
        EXECUTE format('ALTER TABLE shift_downtime_reason DROP CONSTRAINT %I', con.conname);
    END LOOP;

    ALTER TABLE shift_downtime_reason
        ADD CONSTRAINT shift_downtime_reason_minutes_check
        CHECK (minutes > 0 AND minutes <= 720);
END
$$;


-- ===========================================================================
-- VERIFICATION — run these individually.
-- pgAdmin only shows the LAST result set when a whole file is executed.
-- ===========================================================================

-- The new table exists and the app role can read and write it.
-- Expect one row per privilege: INSERT and SELECT.
SELECT privilege_type
FROM information_schema.role_table_grants
WHERE table_name = 'machine_shift_length' AND grantee = 'trackfox_app'
ORDER BY privilege_type;

-- The cap now reads 720. Expect exactly one row.
SELECT conname, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.shift_downtime_reason'::regclass AND contype = 'c';
