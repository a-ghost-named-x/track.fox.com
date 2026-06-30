-- Seed/reference values for the standards table.
-- EDIT THIS FILE to reflect your real machine list and per-slot cumulative
-- targets, then re-run it (it's idempotent via ON CONFLICT) whenever
-- standards change. This is NOT exposed through any web form yet.
--
-- Time slots are cumulative across the day: each slot's standard should be
-- >= the previous slot's standard for the same machine.

-- Example real data given for machine C1 (cumulative units by slot):
INSERT INTO standards (machine_id, time_slot, standard_units) VALUES
    ('C1', '8AM',  11700),
    ('C1', '10AM', 23400),
    ('C1', '12PM', 35100),
    ('C1', '2PM',  46800),
    ('C1', '4PM',  58500),
    ('C1', '6PM',  70200)
    -- TODO: fill in remaining slots for C1: 6PM(overnight)/8PM/10PM/12AM/2AM/4AM
    -- and confirm whether the cumulative count resets at a shift boundary
    -- (e.g. does 6AM start a fresh count, or does it continue from 4AM?)
ON CONFLICT (machine_id, time_slot) DO UPDATE
    SET standard_units = EXCLUDED.standard_units;

-- TODO: add rows for every other machine_id, all 12 time slots each.
-- Placeholder example for a second machine so the dashboard has >1 row to render:
INSERT INTO standards (machine_id, time_slot, standard_units) VALUES
    ('C2', '6AM',  10000),
    ('C2', '8AM',  20000),
    ('C2', '10AM', 30000),
    ('C2', '12PM', 40000),
    ('C2', '2PM',  50000),
    ('C2', '4PM',  60000),
    ('C2', '6PM',  70000),
    ('C2', '8PM',  80000),
    ('C2', '10PM', 90000),
    ('C2', '12AM', 100000),
    ('C2', '2AM',  110000),
    ('C2', '4AM',  120000)
ON CONFLICT (machine_id, time_slot) DO UPDATE
    SET standard_units = EXCLUDED.standard_units;
