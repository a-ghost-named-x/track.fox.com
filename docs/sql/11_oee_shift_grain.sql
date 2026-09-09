-- track.fox.com v2 — Move OEE capture from 2-hour slots to whole shifts
--
-- WHY
-- ---
-- Per the floor (2026-09-09): collecting downtime and scrap every two hours was
-- adding work to the production rounds. One person now enters the whole shift's
-- OEE data in a single sitting at the end of it, on /console/oee. The 2-hour
-- rounds go back to good units only.
--
-- This is a better fit for the metric as well as for the people. A per-slot
-- delta is the gap between two hand-taken readings, so a late reading borrows
-- units from its neighbour — that noise is what produced the false "impossible
-- value" alarms on WS1 and C8 in the first week. A shift is 480 minutes no
-- matter when anyone wrote anything down, so the noise cancels completely.
--
-- WHAT CHANGES
-- ------------
--   slot_scrap           -> shift_scrap            (time_slot  -> shift)
--   slot_downtime_entry  -> shift_downtime_entry   (time_slot  -> shift)
--   slot_downtime_reason -> shift_downtime_reason
--   machine_schedule     unchanged — it was already per shift
--   downtime_reasons     unchanged here; 12_seed_shift_downtime_reasons.sql
--                        retires the old codes and adds the floor's real ones
--
-- Scrap stops being cumulative. At slot grain it was a running total so that
-- each checkpoint superseded the last; at shift grain there is one number, and
-- it is that shift's total scrap. The carry-forward rule in _cumulative_deltas()
-- now applies only to production units on the 2-hour form.
--
-- HOW TO APPLY
-- ------------
--   psql/pgAdmin as the `postgres` superuser, NOT trackfox_app, and not
--   through CI/CD. Run this file, then 12_seed_shift_downtime_reasons.sql.
--
--   Existing slot-level rows are copied forward, not discarded — see section 3.
--   The old tables are left in place; section 5 drops them, deliberately
--   commented out so you can verify the migration first.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS throughout, and the copy in section 3
-- skips rows already migrated.


-- ===========================================================================
-- GUARD 1: ARE WE EVEN IN THE RIGHT DATABASE?
--
-- This catches the mistake that is by far the easiest to make in pgAdmin:
-- opening a Query Tool from the SERVER node rather than from the trackfox
-- database node connects you to the `postgres` maintenance database, where
-- none of this schema exists. The symptom is a bare 'relation ... does not
-- exist' on a file you know you have already applied.
--
-- `entries` and `standards` are the unambiguous markers of the application
-- database: they predate every OEE change and are never absent from a working
-- install. Checking them separately from the OEE tables is what lets the two
-- failures tell themselves apart — "wrong database" and "06 not applied yet"
-- otherwise produce identical-looking errors.
-- ===========================================================================
DO $$
BEGIN
    IF to_regclass('public.entries') IS NULL
       OR to_regclass('public.standards') IS NULL THEN
        RAISE EXCEPTION
            'This is not the track.fox.com database. Connected to "%" as "%" (search_path "%"), which has no `entries`/`standards` tables. Reconnect to the trackfox database — in pgAdmin, open the Query Tool from the trackfox database node, not from the server node, which defaults to `postgres`.',
            current_database(), current_user, current_setting('search_path');
    END IF;
END
$$;


-- ===========================================================================
-- GUARD 2: HAS 06 BEEN APPLIED?
--
-- shift_downtime_reason below has a foreign key into downtime_reasons, which
-- 06_oee_schema.sql creates. Without this, an out-of-order run dies partway
-- through and leaves a half-created schema with no clue which file to run.
--
-- Reaching here means the database is right, so a failure genuinely does mean
-- 06 has not been applied. It is idempotent — just run it.
-- ===========================================================================
DO $$
BEGIN
    IF to_regclass('public.downtime_reasons') IS NULL THEN
        RAISE EXCEPTION
            'downtime_reasons does not exist in database "%". This IS the right database, so apply docs/sql/06_oee_schema.sql first (it is idempotent), then re-run this file.',
            current_database();
    END IF;
END
$$;


-- ===========================================================================
-- 1. SCRAP, per machine per shift
--
-- One number: total scrap for the shift. NOT cumulative — there is nothing to
-- accumulate against when there is only one reading.
--
-- Append-only like everything else here: a correction is a new row with a
-- later created_at, and readers take the newest per (machine, date, shift).
-- ===========================================================================
CREATE TABLE IF NOT EXISTS shift_scrap (
    id           BIGSERIAL PRIMARY KEY,
    machine_id   TEXT NOT NULL,
    entry_date   DATE NOT NULL,
    shift        TEXT NOT NULL CHECK (shift IN ('1st Shift', '2nd Shift', '3rd Shift')),
    scrap_units  INTEGER NOT NULL CHECK (scrap_units >= 0),
    entered_by   TEXT NOT NULL,   -- free-text employee number, NOT verified identity
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ===========================================================================
-- 2. DOWNTIME, per machine per shift — header plus one child per reason
--
-- Still two tables, for the same reason as before: a shift routinely has more
-- than one cause, and the split is what makes the Pareto on /oee possible.
--
-- The newest header for a (machine, date, shift) wins WHOLESALE, children and
-- all. That is what lets a correction REMOVE a reason entered by mistake —
-- resolving per-reason instead would hit the dead end entries.issue has, where
-- a blank correction cannot retract an earlier value.
--
-- A header with ZERO children is the load-bearing case: it means "ran clean,
-- no downtime, 100% availability". No header at all means nobody has entered
-- this shift yet. Those must never collapse into each other.
--
-- `was_planned` is still snapshotted per child even though every code the
-- floor uses is currently unplanned. If a planned code is ever added, history
-- must not silently re-rate itself — same reasoning as entries.status being
-- stored rather than recomputed.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS shift_downtime_entry (
    id          BIGSERIAL PRIMARY KEY,
    machine_id  TEXT NOT NULL,
    entry_date  DATE NOT NULL,
    shift       TEXT NOT NULL CHECK (shift IN ('1st Shift', '2nd Shift', '3rd Shift')),
    note        TEXT,
    entered_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- minutes > 0, not >= 0: a zero-minute reason says nothing. "Ran clean" is a
-- header with no children, never a child with a zero.
--
-- The cap is one whole shift (480 minutes) rather than one slot's 120.
CREATE TABLE IF NOT EXISTS shift_downtime_reason (
    id                 BIGSERIAL PRIMARY KEY,
    downtime_entry_id  BIGINT NOT NULL REFERENCES shift_downtime_entry(id) ON DELETE CASCADE,
    reason_code        TEXT NOT NULL REFERENCES downtime_reasons(code),
    minutes            INTEGER NOT NULL CHECK (minutes > 0 AND minutes <= 480),
    was_planned        BOOLEAN NOT NULL
);


-- ===========================================================================
-- 3. CARRY THE EXISTING SLOT-LEVEL DATA FORWARD
--
-- Roughly a week of scrap and downtime was captured at slot grain before this
-- change. It is rolled up rather than thrown away.
--
--   scrap    -> the LAST cumulative reading in the shift, which is that
--               shift's total (the column was cumulative and monotonic).
--   downtime -> minutes SUMMED per reason across the shift's four slots.
--
-- WHY THIS IS WRAPPED IN EXISTENCE CHECKS
-- ---------------------------------------
-- The slot_* tables are optional inputs, not requirements. They are absent in
-- two perfectly normal situations: a fresh database that never had slot-grain
-- capture, and an established one where section 5 has already dropped them.
-- Referencing them unconditionally would make this file fail in both — and
-- would break the "idempotent, safe to re-run" promise the header makes, since
-- re-running after section 5 would error rather than no-op.
--
-- The statements therefore run through EXECUTE inside a guard: SQL is parsed
-- when EXECUTE runs, not when the block is compiled, so a missing table is
-- simply skipped with a notice instead of a syntax-time failure.
--
-- Each copy also skips rows already migrated, so running this twice with the
-- slot tables still present changes nothing the second time.
--
-- The migrated rows keep their original entered_by where there is exactly one,
-- and are marked 'migrated' where several people contributed to the same shift.
-- ===========================================================================

DO $migrate_scrap$
BEGIN
    IF to_regclass('public.slot_scrap') IS NULL THEN
        RAISE NOTICE 'slot_scrap not present - no scrap to migrate, skipping.';
        RETURN;
    END IF;

    EXECUTE $sql$
        INSERT INTO shift_scrap (machine_id, entry_date, shift, scrap_units, entered_by, created_at)
        SELECT
            latest.machine_id,
            latest.entry_date,
            m.shift,
            -- Cumulative and monotonic, so the largest value in the shift is
            -- that shift's total.
            max(latest.scrap_cumulative)                                     AS scrap_units,
            CASE WHEN count(DISTINCT latest.entered_by) = 1
                 THEN min(latest.entered_by) ELSE 'migrated' END             AS entered_by,
            max(latest.created_at)                                           AS created_at
        FROM (
            SELECT DISTINCT ON (machine_id, entry_date, time_slot)
                machine_id, entry_date, time_slot, scrap_cumulative, entered_by, created_at
            FROM slot_scrap
            ORDER BY machine_id, entry_date, time_slot, created_at DESC
        ) latest
        JOIN (VALUES
            ('8AM','1st Shift'),('10AM','1st Shift'),('12PM','1st Shift'),('2PM','1st Shift'),
            ('4PM','2nd Shift'),('6PM','2nd Shift'),('8PM','2nd Shift'),('10PM','2nd Shift'),
            ('12AM','3rd Shift'),('2AM','3rd Shift'),('4AM','3rd Shift'),('6AM','3rd Shift')
        ) AS m(time_slot, shift) ON m.time_slot = latest.time_slot
        WHERE NOT EXISTS (
            SELECT 1 FROM shift_scrap s
            WHERE s.machine_id = latest.machine_id
              AND s.entry_date = latest.entry_date
              AND s.shift      = m.shift
        )
        GROUP BY latest.machine_id, latest.entry_date, m.shift
    $sql$;
END
$migrate_scrap$;


DO $migrate_downtime$
BEGIN
    IF to_regclass('public.slot_downtime_entry') IS NULL
       OR to_regclass('public.slot_downtime_reason') IS NULL THEN
        RAISE NOTICE 'slot downtime tables not present - nothing to migrate, skipping.';
        RETURN;
    END IF;

    -- Headers first: one per (machine, date, shift) that had any slot-level
    -- submission, carrying the notes attached to those slots.
    EXECUTE $sql$
        INSERT INTO shift_downtime_entry (machine_id, entry_date, shift, note, entered_by, created_at)
        SELECT
            latest.machine_id,
            latest.entry_date,
            m.shift,
            nullif(string_agg(DISTINCT latest.note, '; '), '')               AS note,
            CASE WHEN count(DISTINCT latest.entered_by) = 1
                 THEN min(latest.entered_by) ELSE 'migrated' END             AS entered_by,
            max(latest.created_at)                                           AS created_at
        FROM (
            SELECT DISTINCT ON (machine_id, entry_date, time_slot)
                id, machine_id, entry_date, time_slot, note, entered_by, created_at
            FROM slot_downtime_entry
            ORDER BY machine_id, entry_date, time_slot, created_at DESC
        ) latest
        JOIN (VALUES
            ('8AM','1st Shift'),('10AM','1st Shift'),('12PM','1st Shift'),('2PM','1st Shift'),
            ('4PM','2nd Shift'),('6PM','2nd Shift'),('8PM','2nd Shift'),('10PM','2nd Shift'),
            ('12AM','3rd Shift'),('2AM','3rd Shift'),('4AM','3rd Shift'),('6AM','3rd Shift')
        ) AS m(time_slot, shift) ON m.time_slot = latest.time_slot
        WHERE NOT EXISTS (
            SELECT 1 FROM shift_downtime_entry e
            WHERE e.machine_id = latest.machine_id
              AND e.entry_date = latest.entry_date
              AND e.shift      = m.shift
        )
        GROUP BY latest.machine_id, latest.entry_date, m.shift
    $sql$;

    -- Then the reason children, minutes summed per code across the shift.
    EXECUTE $sql$
        INSERT INTO shift_downtime_reason (downtime_entry_id, reason_code, minutes, was_planned)
        SELECT
            target.id,
            src.reason_code,
            -- Clamp at a full shift: four slots could in principle each carry
            -- 120 minutes, which this table's CHECK would reject.
            least(sum(src.minutes), 480)  AS minutes,
            bool_or(src.was_planned)      AS was_planned
        FROM (
            SELECT DISTINCT ON (e.machine_id, e.entry_date, e.time_slot)
                e.id, e.machine_id, e.entry_date, e.time_slot
            FROM slot_downtime_entry e
            ORDER BY e.machine_id, e.entry_date, e.time_slot, e.created_at DESC
        ) latest
        JOIN slot_downtime_reason src ON src.downtime_entry_id = latest.id
        JOIN (VALUES
            ('8AM','1st Shift'),('10AM','1st Shift'),('12PM','1st Shift'),('2PM','1st Shift'),
            ('4PM','2nd Shift'),('6PM','2nd Shift'),('8PM','2nd Shift'),('10PM','2nd Shift'),
            ('12AM','3rd Shift'),('2AM','3rd Shift'),('4AM','3rd Shift'),('6AM','3rd Shift')
        ) AS m(time_slot, shift) ON m.time_slot = latest.time_slot
        JOIN shift_downtime_entry target
              ON target.machine_id = latest.machine_id
             AND target.entry_date = latest.entry_date
             AND target.shift      = m.shift
        WHERE NOT EXISTS (
            SELECT 1 FROM shift_downtime_reason r WHERE r.downtime_entry_id = target.id
        )
        GROUP BY target.id, src.reason_code
    $sql$;
END
$migrate_downtime$;


-- ===========================================================================
-- 4. INDEXES AND GRANTS
--
-- Same access pattern as before — "newest row for this machine+date+shift" —
-- so created_at DESC rides along to keep DISTINCT ON an index scan.
-- ===========================================================================
CREATE INDEX IF NOT EXISTS idx_shift_scrap_lookup
    ON shift_scrap (machine_id, entry_date, shift, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shift_scrap_date
    ON shift_scrap (entry_date);

CREATE INDEX IF NOT EXISTS idx_shift_downtime_lookup
    ON shift_downtime_entry (machine_id, entry_date, shift, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_shift_downtime_date
    ON shift_downtime_entry (entry_date);

CREATE INDEX IF NOT EXISTS idx_shift_downtime_reason_entry
    ON shift_downtime_reason (downtime_entry_id);
-- Backs the Pareto: minutes by reason across a date range.
CREATE INDEX IF NOT EXISTS idx_shift_downtime_reason_code
    ON shift_downtime_reason (reason_code);

GRANT SELECT, INSERT ON shift_scrap             TO trackfox_app;
GRANT SELECT, INSERT ON shift_downtime_entry    TO trackfox_app;
GRANT SELECT, INSERT ON shift_downtime_reason   TO trackfox_app;

GRANT USAGE, SELECT ON SEQUENCE shift_scrap_id_seq            TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE shift_downtime_entry_id_seq   TO trackfox_app;
GRANT USAGE, SELECT ON SEQUENCE shift_downtime_reason_id_seq  TO trackfox_app;


-- ===========================================================================
-- VERIFICATION — run these individually.
-- pgAdmin only shows the LAST result set when a whole file is executed, so
-- running the file will hide everything above this line.
-- ===========================================================================

-- Old vs new row counts. The new counts should be lower (four slots collapse
-- into one shift) and must not be zero if the old tables had anything.
SELECT 'slot_scrap'            AS table_name, count(*) FROM slot_scrap
UNION ALL SELECT 'shift_scrap',            count(*) FROM shift_scrap
UNION ALL SELECT 'slot_downtime_entry',    count(*) FROM slot_downtime_entry
UNION ALL SELECT 'shift_downtime_entry',   count(*) FROM shift_downtime_entry
UNION ALL SELECT 'slot_downtime_reason',   count(*) FROM slot_downtime_reason
UNION ALL SELECT 'shift_downtime_reason',  count(*) FROM shift_downtime_reason
ORDER BY table_name;

-- Scrap totals must match end to end. Expect ZERO rows.
SELECT old.machine_id, old.entry_date, old.shift,
       old.total AS slot_total, new.scrap_units AS shift_total
FROM (
    SELECT latest.machine_id, latest.entry_date, m.shift,
           max(latest.scrap_cumulative) AS total
    FROM (
        SELECT DISTINCT ON (machine_id, entry_date, time_slot)
            machine_id, entry_date, time_slot, scrap_cumulative
        FROM slot_scrap
        ORDER BY machine_id, entry_date, time_slot, created_at DESC
    ) latest
    JOIN (VALUES
        ('8AM','1st Shift'),('10AM','1st Shift'),('12PM','1st Shift'),('2PM','1st Shift'),
        ('4PM','2nd Shift'),('6PM','2nd Shift'),('8PM','2nd Shift'),('10PM','2nd Shift'),
        ('12AM','3rd Shift'),('2AM','3rd Shift'),('4AM','3rd Shift'),('6AM','3rd Shift')
    ) AS m(time_slot, shift) ON m.time_slot = latest.time_slot
    GROUP BY latest.machine_id, latest.entry_date, m.shift
) old
JOIN (
    SELECT DISTINCT ON (machine_id, entry_date, shift)
        machine_id, entry_date, shift, scrap_units
    FROM shift_scrap
    ORDER BY machine_id, entry_date, shift, created_at DESC
) new USING (machine_id, entry_date, shift)
WHERE old.total <> new.scrap_units;


-- ===========================================================================
-- 5. RETIRING THE OLD TABLES — deliberately commented out.
--
-- Run this ONLY after the verification above looks right and /oee has been
-- checked against a known day. Nothing in the app reads these tables once the
-- new code is deployed, so leaving them in place costs nothing but clutter,
-- and they are the only copy of the pre-migration data.
-- ===========================================================================
-- DROP TABLE IF EXISTS slot_downtime_reason;
-- DROP TABLE IF EXISTS slot_downtime_entry;
-- DROP TABLE IF EXISTS slot_scrap;
