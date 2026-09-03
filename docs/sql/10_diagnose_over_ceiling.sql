-- track.fox.com v2 — Diagnosing "above the derived ceiling" on /oee (READ ONLY)
--
-- Nothing here writes. Safe to run against production any time.
--
-- ---------------------------------------------------------------------------
-- WHAT THIS IS FOR
-- ---------------------------------------------------------------------------
-- /oee compares each slot's production against a per-machine ceiling held in
-- machine_ideal_rates. That ceiling is DERIVED, not measured:
--
--     ideal_units_per_hour = standards.standard_units at '8AM' / 1.5
--
-- which encodes two assumptions:
--
--   1. the '8AM' standard is exactly ONE 2-hour slot's worth of target output
--      (true only if standards reset each shift and ramp 1x/2x/3x/4x), and
--   2. a standard is exactly 75% of theoretical maximum.
--
-- If a machine is reported as beating a ceiling its operators say is real
-- production, one of those two assumptions is wrong FOR THAT MACHINE. Query 1
-- tells you which.
-- ---------------------------------------------------------------------------


-- ===========================================================================
-- 1. THE SHARPEST CHECK — run this first.
--
-- Every row must read 75.0. That is the whole 75%-of-theoretical assumption,
-- stated as a number.
--
--   75.0        -> the ceiling is consistent with the standard. The machine
--                  really did out-produce the derived maximum, so assumption 2
--                  (the 75% figure) is wrong for this machine.
--
--   above 75.0  -> machine_ideal_rates is TOO LOW relative to the current
--                  standard, so the ceiling is too tight and normal production
--                  trips it. Almost always means `standards` changed after
--                  08_seed_ideal_rates.sql was last run. Fix: re-run 08.
--
--   missing row -> no ideal rate seeded; /oee shows no OEE for that machine.
-- ===========================================================================
SELECT
    s.machine_id,
    s.standard_units                                  AS standard_per_slot,
    r.ideal_units_per_hour,
    (r.ideal_units_per_hour * 2)::int                 AS ceiling_per_slot,
    round(100.0 * s.standard_units / (r.ideal_units_per_hour * 2), 1)
                                                      AS pct_of_ceiling
FROM standards s
JOIN machine_ideal_rates r ON r.machine_id = s.machine_id
WHERE s.time_slot = '8AM'
ORDER BY pct_of_ceiling DESC, s.machine_id;


-- ===========================================================================
-- 2. Are the standards themselves still a clean 1x/2x/3x/4x ramp?
--
-- Assumption 1 above. Expect ZERO rows. Any row here means the '8AM' value is
-- not one slot's increment for that machine, so every number derived from it
-- is wrong — including its ceiling.
--
-- This is the same check as PRE-FLIGHT #2 in 08_seed_ideal_rates.sql. Worth
-- re-running standalone, because pgAdmin only displays the LAST result set
-- when a whole file is executed, so it is easy to have missed it.
-- ===========================================================================
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
SELECT s.machine_id, s.time_slot, s.standard_units,
       i.units * p.n AS expected
FROM standards s
JOIN machine_increment i ON i.machine_id = s.machine_id
JOIN slot_position     p ON p.time_slot  = s.time_slot
WHERE s.standard_units <> i.units * p.n
ORDER BY s.machine_id, p.n;


-- ===========================================================================
-- 3. THE RAW CHECKPOINT SEQUENCE — run this before drawing any conclusion.
--
-- All 12 checkpoints for one day, laid out left to right, one row per machine.
-- EDIT THE DATE and the machine list below.
--
-- units_produced is CUMULATIVE WITHIN A SHIFT and resets to zero at every
-- changeover, so a correct row looks like three separate ramps:
--
--     8AM    10AM   12PM   2PM  | 4PM    6PM    8PM   10PM | 12AM ...
--     11700  23400  35100  46800| 11500  23000  34500  46000| 11800 ...
--     \______ 1st shift ________/\______ 2nd shift ________/\___ 3rd ...
--
-- The failure mode to look for is a row that keeps climbing straight through
-- 2PM into 4PM without resetting:
--
--     11700  23400  35100  46800| 58300  69800  81300  92800| ...
--
-- That is someone entering cumulative-for-the-DAY into a field that means
-- cumulative-for-the-SHIFT. The 4PM value then reads as a single slot's
-- output, which is why it lands far over the ceiling.
--
-- If that is what happened, note that /oee is not the only thing affected:
-- `standards` also resets each shift, so compute_status() would be comparing a
-- whole day's units against one shift's target and painting those cells green
-- on the boards regardless of real performance. Worth checking /dashboard for
-- 2nd and 3rd shift if you see this shape.
-- ===========================================================================
WITH target AS (
    SELECT DATE '2026-09-03' AS entry_date   -- <-- EDIT THIS DATE
),
latest AS (
    SELECT DISTINCT ON (e.machine_id, e.time_slot)
           e.machine_id, e.time_slot, e.units_produced
    FROM entries e
    JOIN target t ON t.entry_date = e.entry_date
    ORDER BY e.machine_id, e.time_slot, e.created_at DESC
)
SELECT
    machine_id,
    max(units_produced) FILTER (WHERE time_slot = '8AM')  AS "8AM",
    max(units_produced) FILTER (WHERE time_slot = '10AM') AS "10AM",
    max(units_produced) FILTER (WHERE time_slot = '12PM') AS "12PM",
    max(units_produced) FILTER (WHERE time_slot = '2PM')  AS "2PM",
    max(units_produced) FILTER (WHERE time_slot = '4PM')  AS "4PM",
    max(units_produced) FILTER (WHERE time_slot = '6PM')  AS "6PM",
    max(units_produced) FILTER (WHERE time_slot = '8PM')  AS "8PM",
    max(units_produced) FILTER (WHERE time_slot = '10PM') AS "10PM",
    max(units_produced) FILTER (WHERE time_slot = '12AM') AS "12AM",
    max(units_produced) FILTER (WHERE time_slot = '2AM')  AS "2AM",
    max(units_produced) FILTER (WHERE time_slot = '4AM')  AS "4AM",
    max(units_produced) FILTER (WHERE time_slot = '6AM')  AS "6AM"
FROM latest
-- Drop this WHERE clause to see all 34 machines.
WHERE machine_id IN ('C7', 'C8', 'WS1', 'WS2', 'WS5', 'P4', 'AS4')
GROUP BY machine_id
ORDER BY machine_id;


-- ===========================================================================
-- 4. The actual slot deltas that tripped the flag.
--
-- EDIT THE DATE on the `target AS` line below to the day /oee was showing.
--
-- It is a one-row CTE rather than a psql \set variable on purpose: \set is a
-- psql meta-command and pgAdmin, which is what this project actually uses,
-- sends the backslash straight to the server and fails with a syntax error.
-- Everything in this file is plain SQL and runs in both.
--
-- Read `pct_of_standard` against `pct_of_ceiling`:
--
--   pct_of_standard ~100-130% and pct_of_ceiling just over 100%
--       -> ordinary good production against a ceiling that is too tight.
--          The ceiling is wrong, not the data.
--
--   pct_of_standard in the hundreds or thousands
--       -> a genuine data-entry problem: a transposed digit, or a value typed
--          as cumulative-for-the-DAY rather than cumulative-for-the-SHIFT
--          (units_produced resets every shift).
-- ===========================================================================
WITH target AS (
    SELECT DATE '2026-09-03' AS entry_date   -- <-- EDIT THIS DATE
),
latest AS (
    SELECT DISTINCT ON (e.machine_id, e.entry_date, e.time_slot)
           e.machine_id, e.entry_date, e.time_slot, e.units_produced
    FROM entries e
    JOIN target t ON t.entry_date = e.entry_date
    ORDER BY e.machine_id, e.entry_date, e.time_slot, e.created_at DESC
),
positioned AS (
    SELECT l.*, p.shift_no, p.n
    FROM latest l
    JOIN (VALUES
        ('8AM',  1, 1), ('10AM', 1, 2), ('12PM', 1, 3), ('2PM',  1, 4),
        ('4PM',  2, 1), ('6PM',  2, 2), ('8PM',  2, 3), ('10PM', 2, 4),
        ('12AM', 3, 1), ('2AM',  3, 2), ('4AM',  3, 3), ('6AM',  3, 4)
    ) AS p (time_slot, shift_no, n) ON p.time_slot = l.time_slot
),
windowed AS (
    SELECT machine_id, entry_date, time_slot, units_produced, n,
           lag(units_produced) OVER w AS prev_units,
           lag(n)              OVER w AS prev_n
    FROM positioned
    WINDOW w AS (PARTITION BY machine_id, entry_date, shift_no ORDER BY n)
),
deltas AS (
    SELECT machine_id, entry_date, time_slot, units_produced, prev_units,
           CASE WHEN n = 1 THEN units_produced
                ELSE units_produced - prev_units
           END AS slot_units
    FROM windowed
    WHERE n = 1 OR prev_n = n - 1
)
SELECT
    d.machine_id, d.time_slot,
    d.prev_units                       AS previous_checkpoint,
    d.units_produced                   AS this_checkpoint,
    d.slot_units,
    s.standard_units                   AS standard_per_slot,
    (r.ideal_units_per_hour * 2)::int  AS ceiling_per_slot,
    round(100.0 * d.slot_units / s.standard_units, 1)             AS pct_of_standard,
    round(100.0 * d.slot_units / (r.ideal_units_per_hour * 2), 1) AS pct_of_ceiling
FROM deltas d
JOIN machine_ideal_rates r ON r.machine_id = d.machine_id
JOIN standards s ON s.machine_id = d.machine_id AND s.time_slot = '8AM'
WHERE d.slot_units > r.ideal_units_per_hour * 2
ORDER BY pct_of_ceiling DESC;


-- ===========================================================================
-- 5. THE FIX, once queries 1 and 3 have told you which assumption broke.
--
-- If query 1 showed values ABOVE 75.0 (standards moved since the rates were
-- seeded), re-running the derivation is all that is needed:
--
--     psql ... -f docs/sql/08_seed_ideal_rates.sql
--
-- If query 1 showed exactly 75.0 and query 3 shows ordinary production only
-- slightly over the ceiling, then 75% is simply the wrong figure for those
-- machines. Override just those, recording where the number came from —
-- machine_ideal_rates is keyed per machine for exactly this reason.
--
-- Worked example: a machine whose standard is really 85% of its maximum, not
-- 75%. Its per-slot standard is 10,800, so:
--     theoretical per slot = 10800 / 0.85 = 12,706
--     theoretical per hour = 12706 / 2    =  6,353
--
-- EDIT the machine list and the values before running. This is left commented
-- out deliberately — it is the one statement in this file that writes.
-- ===========================================================================
-- INSERT INTO machine_ideal_rates (machine_id, ideal_units_per_hour, note)
-- VALUES
--     ('C7', 6353.00, 'Standard is 85% of max per the floor, 2026-09-XX - not the 75% default'),
--     ('C8', 6353.00, 'Standard is 85% of max per the floor, 2026-09-XX - not the 75% default')
-- ON CONFLICT (machine_id) DO UPDATE
--     SET ideal_units_per_hour = EXCLUDED.ideal_units_per_hour,
--         note                 = EXCLUDED.note,
--         updated_at           = now();
