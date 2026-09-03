-- track.fox.com v2 — Per-machine ideal (theoretical maximum) rate seed
--
-- Populates machine_ideal_rates, which supplies the denominator for OEE's
-- Performance factor. Derived from the existing `standards` table rather than
-- transcribed by hand, so it cannot pick up a typo the way the AS1-AS5
-- spreadsheet did (see the correction note in 04_seed_new_machines_standards.sql).
--
-- HOW TO APPLY:
--   psql/pgAdmin as the `postgres` superuser — trackfox_app has SELECT only
--   on machine_ideal_rates by design. Not through CI/CD.
--   Run 06_oee_schema.sql first.
--
-- Idempotent: ON CONFLICT updates in place. Re-run it after any change to
-- `standards` — this is the deliberate, manual re-sync point. machine_ideal_rates
-- is NOT a view and NOT a generated column, precisely so that editing a
-- management target does not silently move every historical OEE number. You
-- decide when the two get reconciled.
--
-- ---------------------------------------------------------------------------
-- THE ARITHMETIC
-- ---------------------------------------------------------------------------
-- Per the floor: a standard is 75% of the machine's 100% theoretical maximum.
--
-- `standards` is cumulative and resets each shift, so the FIRST slot of any
-- shift holds exactly one 2-hour slot's worth of target good units. (8AM, 4PM
-- and 12AM all carry that same value — the checks below prove it.)
--
--     increment            = standard_units at '8AM'      -- good units / 2h at target
--     theoretical per slot = increment / 0.75              -- good units / 2h at 100%
--     theoretical per hour = increment / 0.75 / 2
--                          = increment / 1.5
--
-- C1: 11,700 / 1.5 = 7,800 units/hour = 130 units/minute.
--
-- ---------------------------------------------------------------------------
-- WORKED EXAMPLE — C1, one 10AM slot
-- ---------------------------------------------------------------------------
--   good (units_produced delta) 9,000     scrap 200     downtime 20 min unplanned
--
--   Planned Production Time = 120 min   (machines run through breaks, so a
--                                        scheduled slot is the full 120)
--   Run Time                = 100 min
--   Total count             = 9,000 + 200 = 9,200
--
--   Availability = 100 / 120                  = 83.33%
--   Performance  = 9,200 / (130 x 100)        = 70.77%
--   Quality      = 9,000 / 9,200              = 97.83%
--   OEE          = 0.8333 x 0.7077 x 0.9783   = 57.69%
--
--   Cross-check, because Run Time cancels out of A x P x Q entirely:
--     OEE = good / (ideal_rate x Planned Production Time)
--         = 9,000 / (130 x 120) = 9,000 / 15,600 = 57.69%   OK
--
--   Second cross-check, in terms of the standard:
--     OEE = 0.75 x (good / standard) = 0.75 x (9,000 / 11,700) = 57.69%   OK
--
-- Both identities are free invariants worth asserting in tests. If A x P x Q
-- ever drifts from good / (ideal_rate x PPT), something is wrong.
--
-- ---------------------------------------------------------------------------
-- THE ANCHOR EVERYONE WILL ASK ABOUT
-- ---------------------------------------------------------------------------
-- A machine that hits standard exactly, with zero scrap and zero downtime,
-- scores 75% OEE — not 100%. That is not a bug; the standard is 75% of max by
-- definition, so the missing 25% shows up as a Performance loss. Tell whoever
-- reads the dashboard this BEFORE they see a 72% and assume the line is
-- broken. For reference, 85% OEE is generally considered world class and 60%
-- is typical for discrete manufacturing.
--
-- If the floor later revises the 75% assumption for one zone (say Poly is
-- really 80% of max), override just those machines with an explicit INSERT
-- below the derived one and record the reasoning in `note` — the table is
-- keyed per machine specifically so that is possible.
-- ---------------------------------------------------------------------------


-- ===========================================================================
-- PRE-FLIGHT CHECKS — run these FIRST. Every one should return ZERO rows.
-- A non-empty result means the derivation below would be wrong, so stop and
-- fix `standards` rather than seeding a bad rate.
-- ===========================================================================

-- 1. The first slot of each shift must carry the same value for a machine.
--    If these disagree, "the 8AM value is one slot's increment" is false.
SELECT machine_id,
       max(standard_units) FILTER (WHERE time_slot = '8AM')  AS at_8am,
       max(standard_units) FILTER (WHERE time_slot = '4PM')  AS at_4pm,
       max(standard_units) FILTER (WHERE time_slot = '12AM') AS at_12am
FROM standards
WHERE time_slot IN ('8AM', '4PM', '12AM')
GROUP BY machine_id
HAVING count(DISTINCT standard_units) > 1;

-- 2. Every shift must ramp 1x / 2x / 3x / 4x off that increment. This is the
--    check that would have caught the AS1-AS5 "5,030 should be 5,040" typo.
WITH machine_increment AS (
    SELECT machine_id, standard_units AS units
    FROM standards
    WHERE time_slot = '8AM'
),
slot_position AS (
    SELECT * FROM (VALUES
        ('8AM', 1), ('10AM', 2), ('12PM', 3), ('2PM', 4),
        ('4PM', 1), ('6PM',  2), ('8PM',  3), ('10PM', 4),
        ('12AM', 1), ('2AM', 2), ('4AM',  3), ('6AM',  4)
    ) AS t (time_slot, n)
)
SELECT s.machine_id, s.time_slot, s.standard_units, i.units * p.n AS expected
FROM standards s
JOIN machine_increment i ON i.machine_id = s.machine_id
JOIN slot_position  p ON p.time_slot  = s.time_slot
WHERE s.standard_units <> i.units * p.n
ORDER BY s.machine_id, p.n;

-- 3. Every machine must have all 12 slots, and there must be 34 machines.
--    Expect exactly one row back: (34, 408).
SELECT count(DISTINCT machine_id) AS machines, count(*) AS slot_rows
FROM standards;

-- 4. Not fatal, but worth knowing: increment / 1.5 is a whole number only when
--    the increment is divisible by 3. Any row here gets a fractional rate,
--    which NUMERIC(10,2) will round.
SELECT machine_id, standard_units, standard_units / 1.5 AS ideal_per_hour
FROM standards
WHERE time_slot = '8AM'
  AND (standard_units * 2) % 3 <> 0;


-- ===========================================================================
-- DEPENDENCY GUARD
--
-- Everything ABOVE this point is read-only, so the pre-flight section is safe
-- to run on its own at any time, before any of the OEE tables exist.
-- Everything BELOW writes, and needs the tables created by 06_oee_schema.sql.
--
-- Without this guard, running the files out of order dies at the INSERT with a
-- bare `relation "machine_ideal_rates" does not exist`, which tells you what
-- broke but not what to go do about it. This says it plainly instead.
-- ===========================================================================
DO $$
BEGIN
    IF to_regclass('public.machine_ideal_rates') IS NULL THEN
        RAISE EXCEPTION
            'machine_ideal_rates does not exist yet. Apply docs/sql/06_oee_schema.sql first, then 07_seed_downtime_reasons.sql, then re-run this file. (The pre-flight SELECTs above this point already ran and are safe - nothing was written.)';
    END IF;
END
$$;


-- ===========================================================================
-- SEED
-- ===========================================================================

INSERT INTO machine_ideal_rates (machine_id, ideal_units_per_hour, note)
SELECT
    machine_id,
    standard_units / 1.5,
    'Derived from standards 8AM increment / 1.5; standard = 75% of theoretical max (floor, 2026-09-03)'
FROM standards
WHERE time_slot = '8AM'
ON CONFLICT (machine_id) DO UPDATE
    SET ideal_units_per_hour = EXCLUDED.ideal_units_per_hour,
        note                 = EXCLUDED.note,
        updated_at           = now();


-- ===========================================================================
-- VERIFICATION — expect 34 rows matching the table below.
-- ===========================================================================

SELECT
    r.machine_id,
    s.standard_units          AS increment_2h,
    s.standard_units * 4      AS shift_target,
    r.ideal_units_per_hour,
    round(r.ideal_units_per_hour / 60, 2) AS ideal_per_minute
FROM machine_ideal_rates r
JOIN standards s
  ON s.machine_id = r.machine_id AND s.time_slot = '8AM'
ORDER BY
    regexp_replace(r.machine_id, '[0-9]+$', ''),
    (regexp_replace(r.machine_id, '^[A-Z]+', ''))::int;

--  machine | increment_2h | shift_target | ideal/hr | ideal/min
--  --------+--------------+--------------+----------+----------
--  AS1-AS5 |        2,520 |       10,080 |    1,680 |    28.00
--  AS6-AS7 |        2,250 |        9,000 |    1,500 |    25.00
--  C1-C5   |       11,700 |       46,800 |    7,800 |   130.00
--  C6-C11  |       10,800 |       43,200 |    7,200 |   120.00
--  C14-C16 |       11,700 |       46,800 |    7,800 |   130.00
--  FM1-FM3 |        8,100 |       32,400 |    5,400 |    90.00
--  P1      |       14,400 |       57,600 |    9,600 |   160.00
--  P2-P4   |       16,200 |       64,800 |   10,800 |   180.00
--  WS1-WS6 |        9,900 |       39,600 |    6,600 |   110.00
--
-- Note the C machines are NOT uniform: C1-C5 and C14-C16 run 11,700 while
-- C6-C11 run 10,800. (CLAUDE.md currently claims the original 23 share one
-- set of targets; they do not, and this is where it shows.)

-- Proof of the anchor: a machine at standard with no losses scores 75%.
-- Every row should read 75.00.
SELECT
    r.machine_id,
    round(100 * (s.standard_units / (r.ideal_units_per_hour * 2)), 2) AS oee_at_standard_pct
FROM machine_ideal_rates r
JOIN standards s
  ON s.machine_id = r.machine_id AND s.time_slot = '8AM'
ORDER BY r.machine_id;
