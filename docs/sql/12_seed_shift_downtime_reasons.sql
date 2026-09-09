-- track.fox.com v2 — The floor's real downtime reason codes
--
-- Replaces the placeholder set seeded in 07_seed_downtime_reasons.sql with the
-- eleven codes actually used on the floor's own tracking sheet (confirmed
-- 2026-09-09). Matching their existing sheet is the point: the entry form is
-- meant to mirror how downtime is already being written down, not to teach a
-- new vocabulary.
--
-- HOW TO APPLY
--   psql/pgAdmin as the `postgres` superuser — trackfox_app has SELECT only on
--   this table by design, same as `standards`. Not through CI/CD.
--   Run 11_oee_shift_grain.sql first.
--
-- Idempotent: ON CONFLICT updates in place.
--
-- ---------------------------------------------------------------------------
-- THE OLD CODES ARE RETIRED, NOT DELETED
-- ---------------------------------------------------------------------------
-- shift_downtime_reason.reason_code is a foreign key into this table, and the
-- rows migrated from slot grain still point at the old codes. Deleting them
-- would either fail on the constraint or orphan real history.
--
-- `active = false` is exactly the mechanism for this: the entry form only
-- offers active codes, while /oee can still label historical rows correctly.
--
-- ---------------------------------------------------------------------------
-- ALL ELEVEN ARE UNPLANNED, AND THAT COSTS NOTHING
-- ---------------------------------------------------------------------------
-- Planned downtime is subtracted from Planned Production Time instead of
-- counting as an availability loss. With no planned codes in the set, PPT is
-- always the full shift — 480 minutes for a completed one.
--
-- That is not a compromise, because of how this floor actually schedules:
-- planned maintenance is given a WHOLE SHIFT, never a slice of one. So it is
-- never a downtime *reason* at all — it is a machine that was not scheduled to
-- run, which is recorded by unticking Scheduled on /console/oee and removes
-- the machine from that shift's rollup entirely (not 0%, absent).
--
-- The machine_schedule table IS the planned-downtime mechanism here. These
-- eleven codes only ever describe time lost DURING a shift the machine was
-- expected to run, and all of that is a genuine availability loss.
--
-- is_planned is still honoured end to end, and shift_downtime_reason.was_planned
-- snapshots it per row, so adding a planned code later would work and would not
-- re-rate history. There is simply no need for one.
--
-- Roll Change and Setup stay unplanned on the usual reasoning as well: setup
-- and adjustment is one of the Six Big Losses, and calling it planned would
-- raise every score while hiding changeover as an improvement target.
-- ---------------------------------------------------------------------------

-- Guards. Two separate checks, because "wrong database" and "file not applied
-- yet" otherwise produce identical-looking 'relation does not exist' errors,
-- and the first is the easier mistake to make: in pgAdmin, opening a Query
-- Tool from the SERVER node connects to the `postgres` maintenance database
-- rather than to trackfox.
DO $$
BEGIN
    IF to_regclass('public.entries') IS NULL
       OR to_regclass('public.standards') IS NULL THEN
        RAISE EXCEPTION
            'This is not the track.fox.com database. Connected to "%" as "%", which has no `entries`/`standards` tables. Reconnect to the trackfox database — in pgAdmin, open the Query Tool from the trackfox database node, not the server node.',
            current_database(), current_user;
    END IF;

    IF to_regclass('public.downtime_reasons') IS NULL THEN
        RAISE EXCEPTION
            'downtime_reasons does not exist in database "%". Apply docs/sql/06_oee_schema.sql first, then 11_oee_shift_grain.sql, then re-run this file.',
            current_database();
    END IF;
END
$$;


-- ===========================================================================
-- 1. Retire the placeholder set. History keeps resolving; the form stops
--    offering them.
-- ===========================================================================
UPDATE downtime_reasons
SET active = false
WHERE code IN ('MATL', 'MECH', 'CHGOVR', 'TOOL', 'ELEC',
               'QUAL', 'OPER', 'OTHER', 'PM', 'NOSCHED');


-- ===========================================================================
-- 2. The floor's eleven, in the order their sheet lists them.
--    sort_order drives the order the entry form renders them, so it should
--    keep matching the paper sheet — someone reading down one and ticking down
--    the other should never have to hunt.
-- ===========================================================================
INSERT INTO downtime_reasons (code, label, is_planned, sort_order, active) VALUES
    ('ROLL_CHANGE',  'Roll Change',           false,  10, true),
    ('SETUP',        'Setup',                 false,  20, true),
    ('FAAR',         'FAAR',                  false,  30, true),
    ('EQUIP_FAIL',   'Equipment Failure',     false,  40, true),
    ('OPER_ADJUST',  'Operator Adjustments',  false,  50, true),
    ('DEFECT_MAT',   'Defective Material',    false,  60, true),
    ('LACK_MAT',     'Lack of Material',      false,  70, true),
    ('LACK_OPER',    'Lack of Operator',      false,  80, true),
    ('DELIVERY',     'Delivery',              false,  90, true),
    ('REGISTRATION', 'Registration',          false, 100, true),
    ('SHIFT_START',  'Start of Shift',        false, 110, true)
ON CONFLICT (code) DO UPDATE
    SET label      = EXCLUDED.label,
        is_planned = EXCLUDED.is_planned,
        sort_order = EXCLUDED.sort_order,
        active     = EXCLUDED.active;


-- ===========================================================================
-- VERIFICATION — run these one at a time. pgAdmin shows only the last result
-- set when a file is executed as a whole.
-- ===========================================================================

-- The dropdown, exactly as /console/oee will render it. Expect 11 rows in the
-- order above, every one is_planned = false.
SELECT sort_order, code, label, is_planned
FROM downtime_reasons
WHERE active
ORDER BY sort_order;

-- Expect 11 active, 10 retired.
SELECT active, count(*) FROM downtime_reasons GROUP BY active ORDER BY active DESC;

-- Any historical rows still pointing at a retired code. Not a problem — this
-- is what `active` exists for — but worth seeing so the Pareto's older
-- entries aren't a surprise.
SELECT r.reason_code, d.label, count(*) AS rows, sum(r.minutes) AS minutes
FROM shift_downtime_reason r
JOIN downtime_reasons d ON d.code = r.reason_code
WHERE NOT d.active
GROUP BY r.reason_code, d.label
ORDER BY minutes DESC;
