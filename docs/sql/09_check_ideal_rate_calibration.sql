-- track.fox.com v2 — Ideal-rate calibration check (READ ONLY)
--
-- Not a migration. Nothing here writes, and it is safe to run against
-- production any time. Run it as `postgres` or as trackfox_app — SELECT is
-- all it needs.
--
-- ---------------------------------------------------------------------------
-- WHAT THIS ANSWERS
-- ---------------------------------------------------------------------------
-- machine_ideal_rates was derived from the floor's statement that a standard
-- is 75% of theoretical maximum (08_seed_ideal_rates.sql). Every OEE number
-- is scaled by that assumption, so it is worth knowing whether 0.75 is a
-- MEASURED nameplate rate or a rule of thumb someone applied when the targets
-- were written.
--
-- The test: has any machine, on its best 2-hour slot ever recorded, come
-- anywhere near the theoretical max we derived for it?
--
--   peak_pct_of_theoretical near 90-100%  -> 0.75 looks real. A machine has
--                                            actually approached the ceiling,
--                                            so the ceiling is credible.
--
--   peak_pct_of_theoretical stuck at ~75% -> suspicious. It means no machine
--                                            has ever beaten standard, which
--                                            is possible but also exactly what
--                                            you would see if the "theoretical
--                                            max" is really just standard with
--                                            a 1.333x multiplier on top of it.
--                                            OEE would then read pessimistically
--                                            forever and 85% would be
--                                            unreachable by construction.
--
--   peak_pct_of_theoretical over 100%     -> the derived ceiling is too LOW.
--                                            A machine has already beaten it,
--                                            so 0.75 is wrong for that machine.
--                                            Override it in machine_ideal_rates.
--
-- Caveat worth stating plainly: this reads PEAK observed output, which is a
-- floor on true capability, never a proof of it. A machine that never got the
-- chance to run flat out will look slower than it is. Treat a low number as a
-- prompt to go ask the floor, not as a measurement.
--
-- It also needs real history to say anything. With only a few days of entries
-- the peaks are noise.
-- ---------------------------------------------------------------------------


-- ===========================================================================
-- Slot deltas. entries.units_produced is CUMULATIVE and resets each shift,
-- so one slot's production is this checkpoint minus the previous one in the
-- same shift, and the first slot of a shift is its own delta.
--
-- Only CONTIGUOUS slots are counted. If a checkpoint was skipped, the next
-- delta spans four hours instead of two and would show up as a fake record
-- peak — so those rows are dropped rather than trusted. That gap is itself a
-- finding; the second query below counts them.
-- ===========================================================================
WITH latest AS (
    SELECT DISTINCT ON (machine_id, entry_date, time_slot)
           machine_id, entry_date, time_slot, units_produced
    FROM entries
    ORDER BY machine_id, entry_date, time_slot, created_at DESC
),
positioned AS (
    SELECT l.machine_id, l.entry_date, l.time_slot, l.units_produced,
           p.shift_no, p.n
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
    SELECT machine_id, entry_date, time_slot,
           CASE WHEN n = 1 THEN units_produced
                ELSE units_produced - prev_units
           END AS slot_units
    FROM windowed
    WHERE n = 1 OR prev_n = n - 1
)
SELECT
    d.machine_id,
    count(*)                                       AS slots_observed,
    s.standard_units                               AS standard_slot,
    (r.ideal_units_per_hour * 2)::int              AS theoretical_slot,
    max(d.slot_units)                              AS peak_slot_observed,
    round(100.0 * max(d.slot_units) / s.standard_units, 1)              AS peak_pct_of_standard,
    round(100.0 * max(d.slot_units) / (r.ideal_units_per_hour * 2), 1)  AS peak_pct_of_theoretical,
    round(avg(d.slot_units), 0)                    AS avg_slot_observed,
    min(d.slot_units)                              AS min_slot_observed
FROM deltas d
JOIN machine_ideal_rates r ON r.machine_id = d.machine_id
JOIN standards s ON s.machine_id = d.machine_id AND s.time_slot = '8AM'
GROUP BY d.machine_id, s.standard_units, r.ideal_units_per_hour
ORDER BY peak_pct_of_theoretical DESC;


-- ===========================================================================
-- IMPOSSIBLE NUMBERS ALREADY IN THE TABLE
--
-- Two things that cannot legitimately happen on a cumulative counter. Both
-- produce plausible-looking wrong OEE rather than an obvious blank, which is
-- why they are worth finding before /oee goes live rather than after.
--
--   NEGATIVE DELTA  - a checkpoint lower than the one before it in the same
--                     shift. Means a typo, or somebody entered a per-slot
--                     number into the cumulative field.
--
--   OVER CEILING    - a delta above the machine's theoretical max for two
--                     hours. Physically impossible; usually transposed digits.
--
-- Expect zero rows. Anything here is a correction to file through /console,
-- which is append-only, so the fix is a new entry rather than an UPDATE.
-- ===========================================================================
WITH latest AS (
    SELECT DISTINCT ON (machine_id, entry_date, time_slot)
           machine_id, entry_date, time_slot, units_produced
    FROM entries
    ORDER BY machine_id, entry_date, time_slot, created_at DESC
),
positioned AS (
    SELECT l.machine_id, l.entry_date, l.time_slot, l.units_produced,
           p.shift_no, p.n
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
    CASE WHEN d.slot_units < 0 THEN 'NEGATIVE DELTA'
         ELSE 'OVER CEILING'
    END                                AS problem,
    d.machine_id, d.entry_date, d.time_slot,
    d.prev_units                       AS previous_checkpoint,
    d.units_produced                   AS this_checkpoint,
    d.slot_units,
    (r.ideal_units_per_hour * 2)::int  AS theoretical_slot
FROM deltas d
JOIN machine_ideal_rates r ON r.machine_id = d.machine_id
WHERE d.slot_units < 0
   OR d.slot_units > r.ideal_units_per_hour * 2
ORDER BY d.entry_date DESC, d.machine_id, d.time_slot;


-- ===========================================================================
-- MISSING CHECKPOINTS
--
-- Per the floor, slots do not get skipped — this is a critical process. This
-- query is how you find out whether that is actually true, and it is the same
-- rule /oee will use to decide when a shift's OEE is INCOMPLETE rather than
-- low.
--
-- Only counts slots for machines that reported at least once that shift, so a
-- machine legitimately idle all shift is not reported as 4 gaps.
-- ===========================================================================
WITH latest AS (
    SELECT DISTINCT ON (machine_id, entry_date, time_slot)
           machine_id, entry_date, time_slot
    FROM entries
    ORDER BY machine_id, entry_date, time_slot, created_at DESC
),
slot_map AS (
    SELECT * FROM (VALUES
        ('8AM',  '1st Shift'), ('10AM', '1st Shift'), ('12PM', '1st Shift'), ('2PM',  '1st Shift'),
        ('4PM',  '2nd Shift'), ('6PM',  '2nd Shift'), ('8PM',  '2nd Shift'), ('10PM', '2nd Shift'),
        ('12AM', '3rd Shift'), ('2AM',  '3rd Shift'), ('4AM',  '3rd Shift'), ('6AM',  '3rd Shift')
    ) AS t (time_slot, shift)
),
reported AS (
    SELECT l.machine_id, l.entry_date, m.shift, count(*) AS slots_reported
    FROM latest l
    JOIN slot_map m ON m.time_slot = l.time_slot
    GROUP BY l.machine_id, l.entry_date, m.shift
)
SELECT entry_date, shift, machine_id, slots_reported, 4 - slots_reported AS slots_missing
FROM reported
WHERE slots_reported < 4
ORDER BY entry_date DESC, shift, machine_id;
