-- track.fox.com v2 — Downtime reason code seed
--
-- Deliberately a SHORT starting set. The point is to get the floor logging
-- something consistent, then let the Pareto on /oee show which buckets are
-- actually load-bearing and which need splitting. Adding a code later is one
-- INSERT here; splitting a code that everybody has been using for a year is
-- not, so err toward fewer and coarser to begin with.
--
-- Idempotent: ON CONFLICT updates in place, so it is safe to re-run whenever
-- a label changes or a code is added.
--
-- HOW TO APPLY:
--   psql/pgAdmin as the `postgres` superuser — trackfox_app has SELECT only
--   on this table by design (same as `standards`). Not through CI/CD.
--
-- ---------------------------------------------------------------------------
-- WHY is_planned MATTERS, and why changing it later is not free
-- ---------------------------------------------------------------------------
-- Planned downtime comes OUT of Planned Production Time (the OEE denominator).
-- Unplanned downtime stays in and shows up as an Availability loss. So this
-- flag is the difference between "the machine was not supposed to be running"
-- and "the machine failed us."
--
-- slot_downtime_reason.was_planned SNAPSHOTS this value at write time, so
-- flipping a code here does NOT retroactively rewrite history — that is
-- deliberate (same reasoning as entries.status being snapshotted). If you
-- ever genuinely want a reclassification applied backwards, that is a
-- separate, explicit UPDATE against slot_downtime_reason.was_planned, and it
-- will move historical OEE numbers. Do it knowingly or not at all.
--
-- ---------------------------------------------------------------------------
-- NOTES ON SPECIFIC CODES
-- ---------------------------------------------------------------------------
-- CHGOVR is UNPLANNED, per the floor's decision to follow true OEE. Setup and
-- adjustment is one of the Six Big Losses. Classifying it planned would raise
-- every score and hide changeovers as an improvement target — which is
-- exactly backwards if changeover time is something you want to attack.
--
-- There is no BREAK code. Machines do not stop for breaks or lunch here —
-- someone covers the machine so it keeps running — so a break code would
-- never be used, and an unused code in a dropdown is just a mis-click waiting
-- to happen. If that ever changes, add it as is_planned = true.
--
-- NOSCHED covers a machine idling for part of a slot with no demand. A
-- machine not scheduled for a WHOLE SHIFT belongs in machine_schedule
-- instead, which excludes it from the rollup entirely rather than counting
-- 480 minutes of planned downtime.
--
-- OTHER exists so nobody stalls at the dropdown, but it is a diagnostic
-- failure when it gets common. If OTHER climbs the Pareto, read the notes
-- attached to those entries and promote the recurring ones into real codes.
-- ---------------------------------------------------------------------------

-- Dependency guard — this file seeds a table that 06_oee_schema.sql creates.
-- Turns an out-of-order run into an instruction rather than a bare
-- "relation does not exist".
DO $$
BEGIN
    IF to_regclass('public.downtime_reasons') IS NULL THEN
        RAISE EXCEPTION
            'downtime_reasons does not exist yet. Apply docs/sql/06_oee_schema.sql first, then re-run this file.';
    END IF;
END
$$;

INSERT INTO downtime_reasons (code, label, is_planned, sort_order) VALUES
    -- Unplanned: real availability losses, stay in the OEE denominator.
    ('MATL',    'Material wait or shortage',   false,  10),
    ('MECH',    'Mechanical failure',          false,  20),
    ('CHGOVR',  'Changeover / setup',          false,  30),
    ('TOOL',    'Tooling change or failure',   false,  40),
    ('ELEC',    'Electrical / controls fault', false,  50),
    ('QUAL',    'Quality hold / bad material', false,  60),
    ('OPER',    'No operator available',       false,  70),
    ('OTHER',   'Other - see note',            false,  80),

    -- Planned: excluded from Planned Production Time.
    ('PM',      'Planned maintenance',         true,   90),
    ('NOSCHED', 'Not scheduled / no demand',   true,  100)
ON CONFLICT (code) DO UPDATE
    SET label      = EXCLUDED.label,
        is_planned = EXCLUDED.is_planned,
        sort_order = EXCLUDED.sort_order;

-- ---------------------------------------------------------------------------
-- VERIFICATION
-- ---------------------------------------------------------------------------

-- Expect 10 rows: 8 unplanned, 2 planned.
SELECT is_planned, count(*)
FROM downtime_reasons
WHERE active
GROUP BY is_planned
ORDER BY is_planned;

-- The dropdown, in the order the entry form should render it.
SELECT sort_order, code, label, is_planned
FROM downtime_reasons
WHERE active
ORDER BY sort_order;
